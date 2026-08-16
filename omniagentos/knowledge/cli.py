"""Small operator entrypoints for web ingest and admin vault sync."""

from __future__ import annotations

import argparse
from pathlib import Path

import requests

from omniagentos.contracts import default_db_path, default_vault_dir
from omniagentos.db.store import SqliteStore
from omniagentos.knowledge.capabilities import promote_to_estate
from omniagentos.knowledge.config import admin_dsn, dsn
from omniagentos.knowledge.consolidate import gate
from omniagentos.knowledge.embeddings import OllamaEmbedding
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.vault_sync import sync_path
from omniagentos.knowledge.web import ingest_web


def _title_from_html(content: str, fallback: str) -> str:
    start = content.lower().find("<title>")
    end = content.lower().find("</title>", start + 7)
    return content[start + 7 : end].strip() if start >= 0 and end >= 0 else fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniAgentOS knowledge ingestion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    vault_parser = subparsers.add_parser(
        "sync-vault", help="sync vault notes with admin credentials"
    )
    vault_parser.add_argument("path", nargs="?", default=default_vault_dir())
    web_parser = subparsers.add_parser(
        "ingest-web", help="fetch and ingest a web page as agent data"
    )
    web_parser.add_argument("url")
    # Operator-only company->estate capability promotion. This is the production
    # caller of promote_to_estate: the PromotionGate can only be minted by
    # consolidate.gate() or an operator-token API route, so promotion is never on
    # an agent-reachable path. estate_statement is the operator's clean,
    # company-neutral rewrite — promotion refuses without it.
    promote_parser = subparsers.add_parser(
        "promote-capability",
        help="promote a company capability note to estate scope (admin, redacting gate)",
    )
    promote_parser.add_argument("fact_id", type=int)
    promote_parser.add_argument(
        "--estate-statement",
        required=True,
        help="operator-supplied company-neutral rewrite (required; promotion refuses without it)",
    )
    promote_parser.add_argument("--actor", required=True, help="operator id for the audit trail")
    promote_parser.add_argument("--vault-dir", default=default_vault_dir())
    args = parser.parse_args(argv)

    if args.command == "sync-vault":
        if not admin_dsn():
            parser.error("OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN is required for sync-vault")
        with KnowledgeStore(dsn=admin_dsn(), embedder=OllamaEmbedding()) as store:
            results = sync_path(store, Path(args.path))
        print(f"synced {len(results)} vault note(s)")
        return 0

    if args.command == "promote-capability":
        if not admin_dsn():
            parser.error("OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN is required for promote-capability")
        with KnowledgeStore(dsn=admin_dsn(), embedder=OllamaEmbedding()) as store:
            note = promote_to_estate(
                store,
                args.fact_id,
                promotion_gate=gate(),
                actor=args.actor,
                vault_dir=args.vault_dir,
                estate_statement=args.estate_statement,
                ledger_store=SqliteStore(str(default_db_path())),
            )
        print(f"promoted capability fact {args.fact_id} to estate note {note.id}")
        return 0

    response = requests.get(args.url, timeout=30)
    response.raise_for_status()
    with KnowledgeStore(dsn=dsn(), embedder=OllamaEmbedding()) as store:
        result = ingest_web(
            store,
            url=args.url,
            title=_title_from_html(response.text, args.url),
            content=response.text,
        )
    print(f"ingested web episode {result.episode_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
