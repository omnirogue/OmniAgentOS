"""Metacog memory/artifacts context helper for swarm workers (LANE B)."""

from __future__ import annotations

import logging

from omniagentos.contracts import default_db_path

LOG = logging.getLogger(__name__)


def worker_context_block(
    task_title: str,
    task_description: str,
    project_id: str | None = None,
) -> str:
    """Best-effort wrapper around MetacogService.compile_context().

    Appends assemble_context output when trivially available.
    MUST never raise to callers (returns empty string on error).
    """
    try:
        from omniagentos.metacog.service import MetacogService

        db_path = default_db_path()
        svc = MetacogService(db_path=db_path)

        objective = f"{task_title}\n{task_description}".strip()
        packet = svc.compile_context(
            objective=objective,
            project_id=project_id,
        )

        block = ""
        # Only use packet.block if we compiled actual matching artifacts, memories, or skills
        if packet and packet.block and packet.block.strip():
            if packet.artifact_ids or packet.memory_ids or packet.skill_slugs:
                block = packet.block.strip()

        # Optionally append omniagentos/memory assemble output if trivially available.
        try:
            from omniagentos.db.store import SqliteStore
            from omniagentos.memory.assemble import assemble_context
            from omniagentos.memory.store import ConversationStore

            store = SqliteStore(db_path)
            reader = ConversationStore(store)
            if project_id:
                mem_ctx = assemble_context("project", project_id, 4000, reader=reader)
                if mem_ctx and mem_ctx.block and mem_ctx.block.strip():
                    if block:
                        block += f"\n\n{mem_ctx.block.strip()}"
                    else:
                        block = mem_ctx.block.strip()
        except Exception:
            pass

        if block:
            return f"## Memory & artifacts\n\n{block}"
        return ""
    except Exception:
        # Never raise to callers
        LOG.warning("worker_context_block failed silently", exc_info=True)
        return ""
