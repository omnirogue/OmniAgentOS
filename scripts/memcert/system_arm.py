"""memcert ``system`` arm — the production-shape OmniAgentOS memory stack.

What a resuming OmniAgentOS agent actually gets today (DESIGN §5, launch-path
defaults measured 2026-08-12):

1. **memory/assemble block** — ``memory.assemble.assemble_context`` over the
   task scope's conversation turns, at the PRODUCTION defaults
   (``OMNIAGENTOS_MEMORY_BUDGET_TOKENS`` = 1200, ``max_node_turns`` = 12).
   Fixture sessions are ingested chronologically into a scratch sqlite store
   (never the live DB) as user/agent turns; no rolling summary exists because
   no summarizer job has run — which is also true of a fresh production scope.
2. **transcript reading** — recent raw session transcripts under the remaining
   budget (the resume practice: ``sessions/transcript.py`` /
   ``task-resume-index``), rendered by the ``transcript`` arm builder.

The Synapse knowledge leg stays OFF (``OMNIAGENTOS_KNOWLEDGE`` defaults 0 on
the launch path) and metacog lessons stay OFF (``OMNIAGENTOS_MEMORY_LESSONS``
default) — the arm measures the stack as configured, not as aspirational.
Scratch DBs are cached per world_dir; everything is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
for _p in (str(_SCRIPT_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from . import arms as _arms
    from . import core
except ImportError:  # pragma: no cover - bare-script execution path
    import arms as _arms  # type: ignore[no-redef]
    import core  # type: ignore[no-redef]

from omniagentos.db.migrate import migrate  # noqa: E402
from omniagentos.db.store import SqliteStore  # noqa: E402
from omniagentos.memory.assemble import assemble_context  # noqa: E402
from omniagentos.memory.store import ConversationStore  # noqa: E402

PROD_MEMORY_BUDGET_TOKENS = 1200  # memory/config.py default
PROD_MAX_NODE_TURNS = 12  # memory/config.py default

_STORE_CACHE: dict[str, tuple[SqliteStore, str]] = {}


def _world_key(world_dir: Path) -> str:
    return hashlib.sha256(str(world_dir.resolve()).encode("utf-8")).hexdigest()[:16]


def _iter_session_messages(world_dir: Path) -> list[tuple[str, str, str, str]]:
    """Yield (session_id, role, text, timestamp) chronologically; canaries skipped."""
    sessions_dir = world_dir / "sessions"
    root = sessions_dir if sessions_dir.is_dir() else world_dir
    out: list[tuple[str, str, str, str]] = []
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == "canary":
                continue
            msg = rec.get("message") or {}
            role = msg.get("role") or rec.get("type") or ""
            content = msg.get("content")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                ).strip()
            else:
                text = str(content or rec.get("text") or "").strip()
            stamp = str(rec.get("timestamp") or "")
            if role in ("user", "human"):
                out.append((path.stem, "user", text, stamp))
            elif role in ("assistant", "agent"):
                out.append((path.stem, "agent", text, stamp))
    return out


def _ingested_store(world_dir: Path) -> tuple[SqliteStore, str]:
    """Scratch sqlite store with the world's sessions ingested once (cached).

    The fixture's VIRTUAL timestamp rides in ``meta.ts`` — ``created_at`` is
    the (real) ingestion wall clock, which says nothing about when a fixture
    turn was "said". The hybrid assembler's temporal stamps prefer ``meta.ts``
    for exactly this transcript-import shape.
    """
    key = _world_key(world_dir)
    if key in _STORE_CACHE:
        return _STORE_CACHE[key]
    scratch = Path(tempfile.mkdtemp(prefix=f"memcert-system-{key}-"))
    db_path = scratch / "memory.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    conv = ConversationStore(store)
    scope_id = f"memcert-{key}"
    for session_id, role, text, stamp in _iter_session_messages(world_dir):
        meta: dict[str, Any] = {"session": session_id}
        if stamp:
            meta["ts"] = stamp
        conv.append_turn("task", scope_id, role, text, meta=meta)
    _STORE_CACHE[key] = (store, scope_id)
    return _STORE_CACHE[key]


def build_system_context(
    world_dir: Path,
    item: Any,
    budget_tokens: int,
    rng: Any,
    *,
    hybrid: bool = True,
) -> core.ArmContext:
    """Compose the production-shape context: assemble block + recent transcripts.

    ``hybrid`` pins the assembler mode for THIS call via assemble_context's
    explicit ``hybrid`` parameter: ``True`` measures the LAUNCH-PATH shape
    (launch-env.sh exports OMNIAGENTOS_MEMORY_HYBRID=1; the ``system`` arm),
    ``False`` the v1 assembler (the ``system_legacy`` arm — the paired A/B
    baseline). Never pinned via os.environ: run_bench builds contexts from a
    ThreadPoolExecutor, and env mutation races across pool threads and
    cross-contaminates arms. Everything else is identical between the two arms
    by construction, so a paired delta isolates the hybrid change.
    """
    store, scope_id = _ingested_store(world_dir)
    conv = ConversationStore(store)
    assembled = assemble_context(
        "task",
        scope_id,
        min(PROD_MEMORY_BUDGET_TOKENS, budget_tokens),
        reader=conv,
        max_node_turns=PROD_MAX_NODE_TURNS,
        task_text=item.question,
        hybrid=hybrid,
    )
    remaining = max(budget_tokens - assembled.estimated_tokens, 0)
    transcript_ctx = _arms.build_context("transcript", world_dir, item, remaining, rng)
    parts = [p for p in (assembled.block, transcript_ctx.context_block) if p]
    block = "\n\n".join(parts)
    arm_name = "system" if hybrid else "system_legacy"
    return core.ArmContext(
        arm=arm_name,
        context_block=block,
        meta={
            "chars": len(block),
            "approx_tokens": len(block) // 4,
            "truncated": bool(transcript_ctx.meta.get("truncated")),
            "sources": ["memory.assemble", "transcript"],
            "assemble_tokens": assembled.estimated_tokens,
            "assemble_history_hits": assembled.history_hits,
            "transcript_meta": transcript_ctx.meta,
        },
    )
