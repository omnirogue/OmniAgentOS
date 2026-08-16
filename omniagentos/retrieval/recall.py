"""Unified memory recall over conversation, knowledge, metacog, vault, and memlife.

Five independent stores hold different kinds of memory. Callers should not need to
know which store to ask. :func:`recall` fans out to every available leg, fuses their
rankings with :mod:`omniagentos.retrieval.fusion` (Reciprocal Rank Fusion — the ONE
rank-fusion implementation), and returns a single ``list[RecallLine]``.

Each backend is independently optional and never-raising: a dead leg degrades the
fusion; it never fails the query. Importing this module does not hard-require
Postgres drivers — the knowledge leg is imported lazily and skipped when
``OMNIAGENTOS_KNOWLEDGE`` is off or the store is unavailable.

Authorization (U-M1)
--------------------
When ``holder`` is provided (a canonical lane identity like ``lane:runner.step``),
each backend leg is checked against the LaneProfile registry before querying. Denied
legs are skipped; allowed legs are queried as normal. When ``holder`` is ``None``
(the default), all available legs are queried (backward compatible with today's
behavior).

Enforcement fails CLOSED. Passing ``holder`` is an opt-in to enforcement, so if
the LaneProfile registry itself raises, the leg is denied and the fault is
logged — never authorized "for availability". A leg this module cannot serve
already contributes nothing, so denying costs the query no more than the dead
leg it was already prepared to tolerate.

Scope
-----
``scope=(scope_type, scope_id)`` is applied to the **conversation** leg only
(``ConversationStore.recent_turns``). Knowledge, metacog, vault, and memlife have
no shared scope concept and ignore it. When ``scope`` is ``None``, the
conversation leg returns empty (``recent_turns`` requires a concrete scope).

RRF limitation — disjoint identity spaces
-----------------------------------------
:mod:`omniagentos.retrieval.fusion` documents the ideal RRF property: a document
surfaced by two independent backends outranks one surfaced strongly by a single
backend. P3 does **not** realize that property. The five legs emit disjoint id
spaces by construction:

* conversation — ``"{scope_type}:{scope_id}:{seq}"`` (e.g. ``task:tsk_1:3``)
* knowledge — bare integer fact id
* metacog — memory id (e.g. ``mem_abc…``)
* vault — markdown relpath
* memlife — normalized rendered claim text

No two legs can emit the same id, so each fused entry's ``contributions`` map has
exactly one member and fusion degenerates to a fixed ``1/(k+rank)`` interleave
across backends — a deterministic rank interleave, not agreement-boosting. The
fusion module docstring describes the ideal; this identity scheme does not
achieve it. True multi-backend agreement would need a normalized-text secondary
key so the same fact stored in both metacog and vault would share an id. Until
then, the interleave is a defensible merge policy but must not be confused with
cross-backend-agreement RRF promises.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.retrieval import fusion

_LOG = logging.getLogger(__name__)

__all__ = [
    "RecallLine",
    "recall",
]

_BACKEND_NAMES: tuple[str, ...] = (
    "conversation",
    "knowledge",
    "metacog",
    "vault",
    "memlife",
)

# Cap on RecallLine.text — matches memory/assemble.py's recall-injection budget.
_MAX_RECALL_CHARS = 400

# Applied to the conversation leg only (see module docstring).
_scope: ContextVar[tuple[str, str] | None] = ContextVar("unified_recall_scope", default=None)

# Test hooks: when set, replace the production store/search path for that leg.
_conversation_store_factory: Callable[[], Any] | None = None
_metacog_store_factory: Callable[[], Any] | None = None
_knowledge_search: Callable[[str, int], Sequence[Any]] | None = None
_vault_search: Callable[[str, int], Sequence[Any]] | None = None


@dataclass(frozen=True)
class RecallLine:
    """One fused recall hit ready for a caller to display or inject."""

    text: str
    source: str  # "conversation" | "knowledge" | "metacog" | "vault" | "memlife"
    score: float
    ref: str | None = None


# ---------------------------------------------------------------------------
# Backend search legs (each never-raising; empty list on any failure)
# ---------------------------------------------------------------------------


def _search_conversation(query: str, limit: int) -> list[Any]:
    """Recent conversation turns for the active scope, newest first.

    ``query`` is accepted for BackendSpec parity; ConversationStore has no text
    filter — ranking is recency (ORDER BY seq DESC).
    """
    del query  # unused; signature fixed by BackendSpec
    scope = _scope.get()
    if scope is None or limit <= 0:
        return []
    scope_type, scope_id = scope
    try:
        if _conversation_store_factory is not None:
            store = _conversation_store_factory()
        else:
            from omniagentos.contracts import default_db_path
            from omniagentos.db.store import SqliteStore
            from omniagentos.memory.store import ConversationStore

            store = ConversationStore(SqliteStore(default_db_path()))
        # recent_turns returns chronological (oldest first); fusion wants best-first
        # (newest = rank 1), matching the underlying ORDER BY seq DESC.
        turns = store.recent_turns(scope_type, scope_id, limit)
        return list(reversed(turns))
    except Exception:  # noqa: BLE001 -- one dead leg must not fail the query
        return []


def _conversation_id(turn: Any) -> str:
    scope = _scope.get()
    scope_type, scope_id = scope if scope is not None else ("_", "_")
    seq = getattr(turn, "seq", "?")
    return f"{scope_type}:{scope_id}:{seq}"


def _search_knowledge(query: str, limit: int) -> list[Any]:
    """Semantic facts from Synapse (Postgres+pgvector), when enabled."""
    if _knowledge_search is not None:
        try:
            return list(_knowledge_search(query, limit))
        except Exception:  # noqa: BLE001
            return []
    try:
        # Lazy: config has no psycopg dependency; knowledge.recall / store do.
        from omniagentos.knowledge import config

        if not config.knowledge_enabled():
            return []
        from omniagentos.knowledge.recall import _get_store
        from omniagentos.knowledge.recall import recall as knowledge_recall

        result = knowledge_recall(_get_store(), prompt=query, run_id=None)
        facts = list(result.facts)
        return facts[:limit] if limit > 0 else facts
    except Exception:  # noqa: BLE001 -- driver missing, PG down, store None, etc.
        return []


def _knowledge_id(recalled: Any) -> str:
    fact = getattr(recalled, "fact", recalled)
    return str(getattr(fact, "id", recalled))


def _search_metacog(query: str, limit: int) -> list[Any]:
    """Lessons / procedures / warnings from metacog SQLite memory records."""
    try:
        if _metacog_store_factory is not None:
            store = _metacog_store_factory()
        else:
            from omniagentos.metacog.store import MetacogStore

            store = MetacogStore()
        return list(
            store.search_memory(
                query,
                company_id="default",
                project_id=None,
                statuses=[
                    "promoted"
                ],  # candidates (shadow/pending) must not bypass the promotion gate
                limit=limit,
            )
        )
    except Exception:  # noqa: BLE001
        return []


def _metacog_id(mem: Any) -> str:
    return str(getattr(mem, "id", mem))


def _search_vault(query: str, limit: int) -> list[Any]:
    """Vaultgraph global search over Map-of-Content markdown summaries."""
    if _vault_search is not None:
        try:
            return list(_vault_search(query, limit))
        except Exception:  # noqa: BLE001
            return []
    try:
        from omniagentos.contracts import default_vault_dir
        from omniagentos.vaultgraph.search import global_search

        vault_dir = default_vault_dir()
        if not Path(vault_dir).is_dir():
            return []
        return list(global_search(query, vault_dir, limit=limit))
    except Exception:  # noqa: BLE001
        return []


def _vault_id(hit: Any) -> str:
    return str(getattr(hit, "relpath", hit))


def _search_memlife(query: str, limit: int) -> list[Any]:
    """ACCEPTED lessons rendered to var/memories/<project>/LESSONS.md."""
    if limit <= 0:
        return []
    try:
        from omniagentos.memlife.render import search_rendered_lessons

        return list(search_rendered_lessons(query, top_k=limit))
    except Exception:  # noqa: BLE001 -- one dead leg must not fail the query
        return []


def _memlife_id(claim: Any) -> str:
    return "memlife:" + " ".join(str(claim).split()).casefold()


def _sanitize(text: str, *, cap: int) -> str:
    """Collapse whitespace, neutralize envelope delimiters, cap length.

    Mirrors ``omniagentos.memory.assemble._sanitize`` locally so retrieval does
    not import across the memory boundary. A hostile payload containing
    ``</prior-context>`` must not emit an intact closing tag that a
    delimiter-trusting caller could treat as ending a prior-context block.
    """
    collapsed = " ".join(str(text).split())
    for token in ("<prior-context>", "</prior-context>"):
        collapsed = collapsed.replace(token, token.replace("<", "‹").replace(">", "›"))
    if len(collapsed) > cap:
        collapsed = collapsed[: cap - 1].rstrip() + "…"
    return collapsed


def _text_from_payload(source: str, payload: Any) -> str:
    """Extract display text from a backend payload (sanitized + length-capped)."""
    if payload is None:
        return ""
    if source == "conversation":
        raw = str(getattr(payload, "content", "") or "")
    elif source == "knowledge":
        fact = getattr(payload, "fact", None)
        if fact is not None:
            raw = str(getattr(fact, "statement", "") or "")
        else:
            raw = str(getattr(payload, "statement", "") or "")
    elif source == "metacog":
        raw = str(getattr(payload, "statement", "") or "")
    elif source == "vault":
        snippet = getattr(payload, "snippet", None)
        if snippet:
            raw = str(snippet)
        else:
            raw = str(getattr(payload, "title", "") or "")
    elif source == "memlife":
        raw = str(payload)
    else:
        raw = str(payload)
    return _sanitize(raw, cap=_MAX_RECALL_CHARS)


def _source_from_entry(entry: fusion.FusedEntry) -> str:
    """Primary source for a fused entry (first contributing backend)."""
    if entry.contributions:
        return next(iter(entry.contributions))
    if entry.payloads:
        return next(iter(entry.payloads))
    return "unknown"


def _line_from_entry(entry: fusion.FusedEntry) -> RecallLine:
    source = _source_from_entry(entry)
    payload = entry.payloads.get(source, entry.payload)
    return RecallLine(
        text=_text_from_payload(source, payload),
        source=source,
        score=float(entry.score),
        ref=entry.id if entry.id else None,
    )


def _ensure_backends() -> None:
    """Register the five default legs if they are not already present.

    Idempotent: tests may pre-register replacements under the same names; those
    are left alone. Missing names are filled with the production search callables.
    """
    existing = fusion.registered_backends()
    specs: list[fusion.BackendSpec] = [
        fusion.BackendSpec(
            name="conversation",
            search=_search_conversation,
            id_fn=_conversation_id,
            start=1,
        ),
        fusion.BackendSpec(
            name="knowledge",
            search=_search_knowledge,
            id_fn=_knowledge_id,
            start=1,
        ),
        fusion.BackendSpec(
            name="metacog",
            search=_search_metacog,
            id_fn=_metacog_id,
            start=1,
        ),
        fusion.BackendSpec(
            name="vault",
            search=_search_vault,
            id_fn=_vault_id,
            start=1,
        ),
        fusion.BackendSpec(
            name="memlife",
            search=_search_memlife,
            id_fn=_memlife_id,
            start=1,
        ),
    ]
    for spec in specs:
        if spec.name not in existing:
            fusion.register_backend(spec)


def _is_authorized(holder: str | None, surface: str, scope: tuple[str, str] | None) -> bool:
    """Check if holder is authorized to read a surface (U-M1 authorization).

    Parameters
    ----------
    holder : str | None
        Canonical lane identity. If None, authorization is skipped (backward
        compat) and a WARNING naming the surface is logged — see GAP-05 below.
        NOTE that no production caller currently supplies one, so this is the
        path every real call takes.
    surface : str
        Memory surface name (conversation, knowledge, metacog, vault, memlife).
    scope : (scope_type, scope_id) tuple or None
        The scope (task, project, user, channel, etc.) being queried.

    Returns
    -------
    bool
        True if authorized (or holder is None); False if denied.

    FAILS CLOSED
    ------------
    This used to answer ``True`` on any exception, with the comment
    "authorization fault degrades to allow (fail-open)" — which means a corrupt
    seed, an import error or a plain bug inside :mod:`omniagentos.context.lanes`
    silently switched the entire deny-by-default boundary off, and every denied
    leg became readable, for exactly as long as nobody noticed. That is the
    defect class this program exists to remove, and availability does not
    justify it here: a caller that passes ``holder`` has ASKED to be enforced,
    and this module's own contract for a leg it cannot serve is already "return
    nothing", not "return everything". So a fault denies the leg and is logged
    at ERROR with the identity and surface, which is a signal an operator can
    act on rather than a silent widening.
    """
    if holder is None:
        # GAP-05, STEP 1 OF 2 — OBSERVE, DO NOT CHANGE. Behaviour here is
        # deliberately identical to before; only the silence is removed.
        #
        # What this bypass actually is: no production caller threads a holder
        # (`toolplane/tools.py` calls `recall(query, scope=..., top_k=...,
        # sources=...)` and nothing else does), so EVERY production call takes
        # this branch and the authorization block below has never executed.
        # The elaborate fail-closed contract documented above guards a door
        # that is already open.
        #
        # And ``None`` is not a neutral default. A real holder is constrained by
        # its LaneProfile; ``None`` is constrained by nothing, so it sits at the
        # TOP of the lattice — it grants strictly more than any designated
        # identity could ever be granted. "No identity supplied" reading as
        # "maximum privilege" is the shape this estate exists to refuse.
        #
        # Closing it today would deny every real call (no caller passes a
        # holder), so the ordering is: make it VISIBLE first, learn which call
        # sites need a real identity, thread them, and only then flip the
        # default — behind a flag, canaried. That is step 2, and it needs the operator's
        # written ruling on authorization semantics before anyone touches it.
        _LOG.warning(
            "recall authorization BYPASSED: no holder supplied for surface %r "
            "(scope=%r). This is the GAP-05 top-of-lattice default — the "
            "authorization check below did not run.",
            surface,
            scope,
            stack_info=True,
        )
        return True  # Backward compat: no authorization check when holder is None.

    try:
        from omniagentos.context.lanes import authorize_memory_access

        receipt = authorize_memory_access(holder, surface, scope, mode="read")
        return receipt.allowed
    except Exception:  # noqa: BLE001 -- a broken authorizer authorizes nothing.
        _LOG.error(
            "recall authorization is unavailable; denying %s access to surface %r "
            "(scope=%r). The LaneProfile registry raised.",
            holder,
            surface,
            scope,
            exc_info=True,
        )
        return False


def recall(
    query: str,
    *,
    scope: tuple[str, str] | None = None,
    top_k: int = 8,
    sources: Sequence[str] | None = None,
    holder: str | None = None,
) -> list[RecallLine]:
    """Fan out to memory backends and return a fused, ranked result list.

    Parameters
    ----------
    query:
        Natural-language or keyword query.
    scope:
        Optional ``(scope_type, scope_id)`` applied to the conversation leg only.
        Other backends ignore scope. When ``None``, conversations are skipped.
    top_k:
        Maximum number of fused lines to return.
    sources:
        Optional subset of backend names
        (``conversation`` / ``knowledge`` / ``metacog`` / ``vault`` / ``memlife``).
        ``None`` means the five default legs (never the entire process registry).
    holder:
        Optional canonical lane identity (e.g., ``lane:runner.step``). When provided,
        each backend is authorized via the LaneProfile registry before querying. When
        ``None`` (the default), all available legs are queried (backward compatible
        with today's behavior).

    Returns
    -------
    list[RecallLine]
        Best-first fused hits. Empty when every leg is unavailable or empty.
        Never raises for backend failures.
    """
    if top_k <= 0:
        return []
    try:
        _ensure_backends()
    except Exception:  # noqa: BLE001 -- registry must not block an empty result
        return []

    # Always pass an explicit name list to fuse_backends. names=None would pull
    # every process-global registration (including unrelated legs registered by
    # other callers). sources=None means the five defaults only.
    known = fusion.registered_backends()
    if sources is not None:
        # Preserve caller order but drop unknown labels; empty selection → [].
        names: list[str] = [n for n in sources if n in known]
    else:
        names = [n for n in _BACKEND_NAMES if n in known]
    if not names:
        return []

    # Apply authorization filter: skip backends that are denied to the holder.
    # When holder is None, all backends are allowed (backward compat).
    if holder is not None:
        authorized_names = [
            name for name in names
            if _is_authorized(holder, name, scope)
        ]
        if not authorized_names:
            return []  # All backends denied.
        names = authorized_names

    token = _scope.set(scope)
    try:
        try:
            entries = fusion.fuse_backends(query, limit=top_k, names=names)
        except Exception:  # noqa: BLE001 -- total fusion failure degrades to []
            return []
    finally:
        _scope.reset(token)

    return [_line_from_entry(entry) for entry in entries[:top_k]]
