"""Never-raising memory wiring for the run-input construction path.

These wrappers are the ONLY thing the runner (and other dispatchers) import. Every one
degrades to a no-op / empty result on any fault — a missing ``conversations`` table
(pre-migration-031), a store without the SQLite seam, or a recall failure — so wiring the
memory layer into run finalization can never turn a COMPLETED run FAILED (the same
guarantee the knowledge runner hook makes).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omniagentos.memory import config


def _conversation_store(store: Any) -> Any | None:
    try:
        from omniagentos.memory.store import ConversationStore

        # ConversationStore composes over SqliteStore's connection/lock; a store without
        # that seam (a pure protocol impl) simply yields no memory rather than crashing.
        return ConversationStore(store)
    except Exception:  # noqa: BLE001
        return None


def safe_memory_block(
    store: Any,
    *,
    task_id: str,
    budget_tokens: int,
    task_text: str | None = None,
    run_id: str | None = None,
    project_id: str | None = None,
    company_id: str | None = None,
) -> tuple[str | None, Any]:
    """Assemble the prior-context block for a task run, or ``None`` if there is nothing.

    Returns (block_text, AssembledContext) tuple. block_text may be None if assembly
    failed or produced an empty block. AssembledContext carries estimated_tokens,
    truncated, node_turns, ancestor_summaries, recalls, has_summary for telemetry.

    General Synapse fact recall remains on the runner's existing knowledge hook. Plan-07
    capability recall is intentionally wired here because it is a distinct, tenant-scoped
    company+estate namespace and must be present for every runner lineage at brief time.

    Metacog memory lessons ARE wired here via ``default_memory_recaller`` so every agent
    lineage (not only Anthropic's memory tool) sees promoted lessons/warnings in the
    shared prior-context block. ``run_id`` is passed to its retrieval telemetry so every
    selected lesson is attributed to this precise run. Recall is best-effort and never
    fails assembly.

    ``task_text`` (the clean task prompt) unconditionally seeds the recall query passed
    to ``memory_recaller`` (and to a knowledge ``recaller``, if one were ever wired here),
    outranking the turns/summary fallback — this is what makes recall reachable on a
    fresh node with no prior turns and no rolling summary. It also drives scored packing
    when ``OMNIAGENTOS_MEMORY_SCORED=1``. ``None`` keeps both the fixed-priority order and
    the turns/summary-derived query (backward compatible with every existing caller).
    """
    from omniagentos.memory.contracts import AssembledContext

    conversation = _conversation_store(store)
    if conversation is None:
        return None, AssembledContext(
            scope_type="task", scope_id=task_id, budget_tokens=budget_tokens
        )
    memory_recaller: Callable[[str, int], list[str]] | None = None
    # Wire memory_recaller only when lessons are enabled.
    if config.lessons_enabled():
        try:
            from omniagentos.memory.memory_bridge import default_memory_recaller

            def _recall_with_attribution(query: str, top_k: int) -> list[str]:
                """Bind this run's identity onto the shared recaller signature."""
                return default_memory_recaller(query, top_k, task_id=task_id, run_id=run_id)

            memory_recaller = _recall_with_attribution
        except Exception:  # noqa: BLE001 -- memory recall is best-effort; omit on import fault.
            memory_recaller = None
    resolved_company = company_id
    capability_budget = 0
    if task_text:
        try:
            from omniagentos.knowledge.capabilities import resolve_company_id
            from omniagentos.knowledge.config import knowledge_enabled

            if knowledge_enabled():
                resolved_company = resolved_company or resolve_company_id(store, project_id)
                capability_budget = min(320, max(0, budget_tokens // 4))
        except Exception:  # noqa: BLE001 -- capability recall is optional.
            capability_budget = 0
    try:
        from omniagentos.memory.assemble import assemble_context

        context = assemble_context(
            "task",
            task_id,
            max(0, budget_tokens - capability_budget),
            reader=conversation,
            memory_recaller=memory_recaller,
            max_node_turns=config.max_node_turns(),
            task_text=task_text,
        )
    except Exception:  # noqa: BLE001
        return None, AssembledContext(
            scope_type="task", scope_id=task_id, budget_tokens=budget_tokens
        )
    if capability_budget and task_text:
        try:
            from omniagentos.contracts import estimate_tokens
            from omniagentos.knowledge.capabilities import safe_ambient_capability_block

            capability_block = safe_ambient_capability_block(
                task_text,
                company_id=resolved_company,
                budget_tokens=capability_budget,
            )
            if capability_block:
                combined = "\n\n".join(part for part in (context.block, capability_block) if part)
                # Preserve the assembler's hard budget contract. Capability recall
                # wins over older conversation material if envelope rounding makes
                # their individually bounded slices exceed the combined cap.
                context.block = (
                    combined if estimate_tokens(combined) <= budget_tokens else capability_block
                )
                context.estimated_tokens = estimate_tokens(context.block)
                context.recalls += sum(
                    line.startswith("[") for line in capability_block.splitlines()
                )
        except Exception:  # noqa: BLE001 -- capability recall never fails assembly.
            pass
    context.budget_tokens = budget_tokens
    return context.block or None, context


def safe_persist_turn(
    store: Any,
    *,
    scope_type: str,
    scope_id: str,
    role: str,
    content: str,
    model: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append a conversation turn on a node; silently no-op on any fault or empty content."""
    if not content or not content.strip():
        return
    conversation = _conversation_store(store)
    if conversation is None:
        return
    try:
        conversation.append_turn(scope_type, scope_id, role, content, model=model, meta=meta)
    except Exception:  # noqa: BLE001
        return


def safe_persist_user_turn(
    store: Any, *, task_id: str, content: str, model: str | None = None
) -> None:
    """Record the operator's brief as a ``user`` turn on the task node."""
    safe_persist_turn(
        store, scope_type="task", scope_id=task_id, role="user", content=content, model=model
    )


def _resolve_chat_for_board_task(store: Any, board_task_id: str) -> str | None:
    """Look up the chat_id whose companion board task matches, or ``None``."""
    try:
        conn = store._connection
        row = conn.execute(
            "SELECT id FROM chats WHERE board_task_id = ? AND status != 'deleted'",
            (board_task_id,),
        ).fetchone()
        return row["id"] if row else None
    except Exception:  # noqa: BLE001
        return None


def _resolve_board_task_id_from_run(store: Any, task_id: str) -> str | None:
    """Walk task_id → latest run → board_task to find a chat companion task.

    The chain: runs.task_id → runs.id → board_tasks.run_id → board_tasks.id.
    Only succeeds when a run exists (runner-based executions; session-mode
    dispatches lack a run and must pass ``board_task_id`` explicitly).
    """
    try:
        conn = store._connection
        run_row = conn.execute(
            "SELECT id FROM runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not run_row:
            return None
        run_id = run_row["id"]
        bt_row = conn.execute(
            "SELECT id, origin FROM board_tasks WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if bt_row and bt_row["origin"] == "chat":
            return bt_row["id"]
        return None
    except Exception:  # noqa: BLE001
        return None


def safe_persist_agent_turn(
    store: Any,
    *,
    task_id: str,
    content: str,
    model: str | None = None,
    board_task_id: str | None = None,
) -> None:
    """Record the agent's result as an ``agent`` turn on the task node.

    When the originating board task came from a chat (``origin='chat'``), the
    turn is ALSO written to ``scope_type='chat', scope_id=<chat_id>`` in the
    same logical operation, so the chat UI reflects agent replies alongside
    user messages.  Deduped by the conversations table's per-scope seq
    allocation (one seq per append, never reused).

    ``board_task_id`` is accepted explicitly for session-mode dispatches that
    lack a run; when omitted, the function attempts to resolve it via the
    runs → board_tasks chain (runner-based executions only).
    """
    safe_persist_turn(
        store, scope_type="task", scope_id=task_id, role="agent", content=content, model=model
    )
    # -- chat reply write-back ---------------------------------------------------
    resolved_btk = board_task_id
    if resolved_btk is None:
        resolved_btk = _resolve_board_task_id_from_run(store, task_id)
    if resolved_btk is None:
        return
    chat_id = _resolve_chat_for_board_task(store, resolved_btk)
    if chat_id is None:
        return
    safe_persist_turn(
        store, scope_type="chat", scope_id=chat_id, role="agent", content=content, model=model
    )


def safe_persist_chat_agent_turn(
    store: Any,
    *,
    chat_id: str,
    content: str,
    model: str | None = None,
    board_task_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write an agent turn to BOTH chat scope and (optionally) the task scope.

    Purpose-built for session-mode chat dispatches where no runner run exists.
    The chat UI reads from ``scope_type='chat'``; the task-scope write keeps
    the memory layer's recall path consistent. ``meta`` (e.g.
    ``{"session_id": …, "turn": …}``) is recorded on the chat-scope turn so
    the chat turn bridge can dedupe concurrent close_turn writers.
    """
    safe_persist_turn(
        store,
        scope_type="chat",
        scope_id=chat_id,
        role="agent",
        content=content,
        model=model,
        meta=meta,
    )
    if board_task_id is not None:
        safe_persist_turn(
            store,
            scope_type="task",
            scope_id=board_task_id,
            role="agent",
            content=content,
            model=model,
        )


__all__ = [
    "safe_memory_block",
    "safe_persist_agent_turn",
    "safe_persist_chat_agent_turn",
    "safe_persist_turn",
    "safe_persist_user_turn",
]
