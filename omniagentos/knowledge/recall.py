"""Hybrid knowledge recall, rendering, and never-raising runner entry points."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import OrderedDict
from itertools import combinations
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.knowledge import config
from omniagentos.knowledge.contracts import (
    RECALL_CHARS_PER_TOKEN,
    RECALL_FOOTER,
    RECALL_HEADER,
    EmbeddingProvider,
    EmbeddingUnavailable,
    Fact,
    FactStatus,
    RecalledFact,
    RecallResult,
)
from omniagentos.knowledge.embeddings import FakeEmbedding, OllamaEmbedding
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.retrieval import fusion

# The ONE rank-fusion implementation (omniagentos.retrieval.fusion); k=60 lives there.
_RRF_K = fusion.RRF_K
# A 0.02/day decay gives a roughly 35-day half-life: recent knowledge gets a useful
# preference without making established, high-trust facts disappear too quickly.
_DECAY_LAMBDA_PER_DAY = 0.02
_QUARANTINE_DISCOUNT = 0.15
# --- Demonstrated-usefulness term (T6.3) -------------------------------------------
# helped_count is incremented once per COMPLETED run for each fact that actually made it
# into the injected block. Until now the column was written and never read; ranking had
# no way to learn that a fact keeps being present when work succeeds.
#
# Shape:  usefulness = min(1 + 0.15 * ln(1 + helped_count), 1.5)
#
# The failure mode this shape is chosen to avoid is rich-get-richer: with a proportional
# term, a fact that helped early ranks higher, therefore gets injected more, therefore
# gets credited more, and eventually crowds out better-matching newer facts on the
# strength of its history alone. Two properties prevent that.
#   1. Logarithmic, so confirmations have sharply diminishing returns — the 200th help is
#      worth ~0.007x what the 1st was. 1 help -> 1.10x, 5 -> 1.27x, 27 -> 1.50x (cap),
#      10_000 -> 1.50x, i.e. IDENTICAL to 27. There is no runaway regime.
#   2. Hard-capped at 1.5x, which is deliberately no larger than the existing
#      multi-signal bonus (1.0 for one retrieval backend -> 1.5 for three). So demonstrated
#      usefulness can re-order facts of comparable relevance, but can never outweigh being
#      independently found by more retrieval signals, and there is a finite, known
#      relevance deficit (1.5x) it can never overcome.
# It also multiplies an existing RRF score, so it can only re-rank facts the retrieval
# legs already surfaced — a much-helped fact that does not match the query is not a
# candidate at all and gets no boost from anywhere.
# At helped_count == 0 the term is EXACTLY 1.0 (log1p(0) == 0.0, and x * 1.0 is x in
# IEEE-754), so a cold knowledge base scores bit-for-bit as it did before this change.
_USEFULNESS_WEIGHT = 0.15
_USEFULNESS_CAP = 1.5
_FAILURE_EVENT_INTERVAL_S = 600.0
_MAX_TRACKED_RUNS = 10_000
# Only the top-scoring recalled facts co-strengthen, bounding Hebbian edge writes to
# O(_MAX_COOCCUR_FACTS^2) per recall instead of O(candidates^2) — a shared-engine
# scalability guard grafted from compete arm B (an uncapped pool made C(50,2)=1225
# separate INSERT+commit round-trips on every run's first step).
_MAX_COOCCUR_FACTS = 8

logger = logging.getLogger(__name__)

_state_lock = RLock()
_knowledge_store: KnowledgeStore | None = None
_knowledge_store_dsn: str | None = None
_knowledge_embed_spec: str | None = None
_audit_store: SqliteStore | None = None
_audit_store_path: str | None = None
_last_failure_ts: float | None = None
_consecutive_failures = 0
_strengthened_pairs: OrderedDict[str, set[tuple[int, int]]] = OrderedDict()
_recall_metadata: dict[str, dict[str, Any]] = {}


def _build_embedder() -> EmbeddingProvider:
    """Build the provider selected by the frozen configuration contract."""
    if config.embed_spec() == "fake":
        return FakeEmbedding()
    return OllamaEmbedding()


def _get_store() -> KnowledgeStore:
    """Return the process-wide agent-role knowledge store."""
    global _knowledge_embed_spec, _knowledge_store, _knowledge_store_dsn
    wanted_dsn = config.dsn()
    wanted_embed_spec = config.embed_spec()
    with _state_lock:
        if (
            _knowledge_store is None
            or _knowledge_store_dsn != wanted_dsn
            or _knowledge_embed_spec != wanted_embed_spec
        ):
            if _knowledge_store is not None:
                _knowledge_store.close()
            _knowledge_store = KnowledgeStore(wanted_dsn, embedder=_build_embedder())
            _knowledge_store_dsn = wanted_dsn
            _knowledge_embed_spec = wanted_embed_spec
        return _knowledge_store


def _get_audit_store() -> SqliteStore:
    """Return the H1 store used for degraded-recall audit events."""
    global _audit_store, _audit_store_path
    wanted_path = default_db_path()
    with _state_lock:
        if _audit_store is None or _audit_store_path != wanted_path:
            _audit_store = SqliteStore(wanted_path)
            _audit_store_path = wanted_path
        return _audit_store


def reset_process_state() -> None:
    """Close and drop the process-wide stores + clear per-run tracking.

    Primarily for test isolation: the recall singleton persists a live PG connection
    across calls, so without a reset a connection interrupted mid-query in one test
    (e.g. by pytest-timeout) poisons every later test in the same process ("another
    command is already in progress"). Also usable operationally to force a reconnect.
    """
    global _knowledge_store, _knowledge_store_dsn, _knowledge_embed_spec
    global _audit_store, _audit_store_path, _last_failure_ts, _consecutive_failures
    with _state_lock:
        if _knowledge_store is not None:
            try:
                _knowledge_store.close()
            except Exception:
                pass
        _knowledge_store = None
        _knowledge_store_dsn = None
        _knowledge_embed_spec = None
        _audit_store = None
        _audit_store_path = None
        _last_failure_ts = None
        _consecutive_failures = 0
        _strengthened_pairs.clear()
        _recall_metadata.clear()


def _visible(
    fact: Fact, discipline: str | None, agent_id: str | None, company_id: str | None
) -> bool:
    """Apply the scope boundary omitted by the frozen candidate SQL."""
    if fact.capability_scope == "estate":
        return True
    if fact.capability_scope == "company":
        return company_id is not None and fact.company_id == company_id
    if fact.capability_scope is not None:
        return False
    if fact.scope == "global":
        return True
    if fact.scope == "discipline":
        return discipline is not None and fact.discipline == discipline
    if fact.scope.startswith("agent:"):
        return agent_id is not None and fact.scope == f"agent:{agent_id}"
    return False


def _graph_ranks(rows: list[dict[str, Any]]) -> dict[int, int]:
    activated = [row for row in rows if float(row.get("graph_activation") or 0.0) > 0.0]
    activated.sort(key=lambda row: (-float(row["graph_activation"]), int(row["id"])))
    return {int(row["id"]): rank for rank, row in enumerate(activated, start=1)}


def _usefulness(helped_count: int) -> float:
    """Bounded, saturating boost from demonstrated usefulness (see _USEFULNESS_CAP).

    Returns exactly 1.0 for a fact that has never been credited, so ranking is unchanged
    on a knowledge base where nothing has helped yet. Defensive against a negative or
    non-integer count arriving from the row (never legitimate; would otherwise produce a
    NaN/domain error inside the hot ranking loop).
    """
    try:
        helped = max(0, int(helped_count))
    except (TypeError, ValueError):
        return 1.0
    return min(1.0 + _USEFULNESS_WEIGHT * math.log1p(helped), _USEFULNESS_CAP)


def _modulated_fact(
    row: dict[str, Any], *, fact: Fact, graph_rank: int | None, now_s: float
) -> RecalledFact | None:
    vector_rank = int(row["vector_rank"]) if row.get("vector_rank") is not None else None
    fts_rank = int(row["fts_rank"]) if row.get("fts_rank") is not None else None
    # Rank fusion itself is shared; the bonus/modulation/quarantine layers below are
    # knowledge-specific and stay here.
    fused = fusion.fuse_ranks(
        {"vector": vector_rank, "fts": fts_rank, "graph": graph_rank}, k=_RRF_K
    )
    if fused is None:
        return None

    rrf = fused.score
    bonus = {1: 1.0, 2: 1.25, 3: 1.5}[fused.backend_count]
    last_access_s = fact.last_accessed.timestamp()
    age_days = max(0.0, (now_s - last_access_s) / 86_400.0)
    usefulness = _usefulness(fact.helped_count)
    modulation = (
        fact.trust
        * (0.5 + 0.5 * fact.importance)
        * math.exp(-_DECAY_LAMBDA_PER_DAY * age_days)
        * usefulness
    )
    domain_bonus = 1.25 if float(row.get("domain_match") or 0.0) > 0.0 else 1.0
    score = rrf * bonus * modulation * domain_bonus
    if fact.status == FactStatus.QUARANTINED:
        # Applied AFTER usefulness on purpose: an unverified fact that has ridden along
        # in many successful runs must still be discounted to quarantine level, never
        # boosted back up to parity with an active fact.
        score *= _QUARANTINE_DISCOUNT

    signals: dict[str, float] = {
        "rrf": rrf,
        "multi_signal_bonus": bonus,
        "usefulness": usefulness,
        "domain_bonus": domain_bonus,
        "modulated": score,
        # vector/fts/graph, only for the legs that surfaced this fact.
        **fused.rank_signals(),
    }
    if graph_rank is not None:
        signals["graph_activation"] = float(row.get("graph_activation") or 0.0)
    return RecalledFact(fact=fact, score=score, signals=signals)


def _strengthen_new_pairs(store: KnowledgeStore, run_id: str, fact_ids: list[int]) -> None:
    """Strengthen each unordered fact pair at most once in this process/run.

    fact_ids arrives score-ordered (best first); only the top _MAX_COOCCUR_FACTS
    participate in co-occurrence strengthening (arm-B graft — bounds edge writes).
    """
    pool = list(dict.fromkeys(fact_ids))[:_MAX_COOCCUR_FACTS]
    pairs = list(combinations(sorted(pool), 2))
    with _state_lock:
        seen = _strengthened_pairs.setdefault(run_id, set())
        _strengthened_pairs.move_to_end(run_id)
        new_pairs = [pair for pair in pairs if pair not in seen]
        for pair in new_pairs:
            # The store method operates on every pair in its input. Passing exactly two
            # ids avoids re-strengthening an old pair that shares one newly recalled id.
            store.strengthen_co_recall([pair[0], pair[1]])
            seen.add(pair)
        while len(_strengthened_pairs) > _MAX_TRACKED_RUNS:
            old_run_id, _ = _strengthened_pairs.popitem(last=False)
            _recall_metadata.pop(old_run_id, None)


def clear_run_state(run_id: str) -> None:
    """Release bounded in-process recall bookkeeping after a terminal run."""
    with _state_lock:
        _strengthened_pairs.pop(run_id, None)
        _recall_metadata.pop(run_id, None)


def last_recall_metadata(run_id: str) -> dict[str, Any]:
    """Return runner metadata produced by the latest successful recall for a run."""
    with _state_lock:
        return dict(_recall_metadata.get(run_id, {}))


def recall(
    store: KnowledgeStore,
    *,
    prompt: str,
    discipline: str | None = None,
    agent_id: str | None = None,
    budget_tokens: int | None = None,
    include_quarantined: bool = False,
    run_id: str | None = None,
    score_floor: float | None = None,
    floor_fraction: float | None = None,
    company_id: str | None = None,
    domains: list[str] | None = None,
    capability_only: bool = False,
    k: int = 50,
) -> RecallResult:
    """Rank one-round-trip hybrid candidates and optionally record runner feedback.

    Passing ``run_id=None`` keeps previews side-effect free. ``None`` floor arguments
    disable injection filtering. A real run bumps all ranked facts, reinforces newly
    co-recalled pairs once per run, and writes one recall log. Presentation truncation
    is deliberately separate from relevance reinforcement.
    """
    started = time.perf_counter()
    query_digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    embedding: list[float] | None = None
    if store._embedder is not None:
        try:
            embedded = store._embedder.embed([prompt])
            embedding = embedded[0] if embedded else None
        except EmbeddingUnavailable:
            # Ollama absence is an expected degraded mode; FTS remains useful.
            embedding = None

    # Capability namespaces are a hard tenant boundary. Graph spread can traverse
    # through an invisible cross-company intermediate and let its topology influence
    # an otherwise visible result, so ambient capability recall uses only the direct
    # vector+FTS legs. Ordinary knowledge recall keeps the configured graph mode.
    mode = "lean" if capability_only else config.recall_mode()
    # Scope is enforced below over the hydrated rows. Querying without the store's broad
    # discipline predicate is necessary so a global-scoped fact never disappears merely
    # because its optional discipline label differs from the current run discipline.
    rows = store.recall_candidates(
        embedding=embedding,
        query_text=prompt,
        discipline=None,
        agent_id=agent_id,
        include_quarantined=include_quarantined,
        mode=mode,
        capability_only=capability_only,
        company_id=company_id,
        domains=domains,
        k=k,
    )
    graph_ranks = _graph_ranks(rows) if mode == "full" else {}
    now_s = time.time()
    ranked: list[RecalledFact] = []
    for row in rows:
        fact = Fact.model_validate(row)
        # Capability facts have their own mandatory two-namespace query. Keeping
        # them out of general recall avoids duplicate injection and prevents a
        # caller that forgot tenant context from accidentally changing semantics.
        if fact.capability_scope is not None and not capability_only:
            continue
        if fact.status not in {FactStatus.ACTIVE, FactStatus.QUARANTINED}:
            continue
        if fact.status == FactStatus.QUARANTINED and not include_quarantined:
            continue
        if not _visible(fact, discipline, agent_id, company_id):
            continue
        recalled = _modulated_fact(
            row,
            fact=fact,
            graph_rank=graph_ranks.get(fact.id),
            now_s=now_s,
        )
        if recalled is not None:
            ranked.append(recalled)
    ranked.sort(key=lambda item: (-item.score, item.fact.id))
    all_ranked = ranked
    relative_floor = (
        floor_fraction * all_ranked[0].score if floor_fraction is not None and all_ranked else None
    )
    injectable = [
        item
        for item in all_ranked
        if (score_floor is None or item.score >= score_floor)
        and (relative_floor is None or item.score >= relative_floor)
    ]
    if logger.isEnabledFor(logging.DEBUG):
        candidate_scores = [item.score for item in all_ranked]
        logger.debug(
            "knowledge recall scores count=%d min=%s median=%s max=%s absolute_floor=%s "
            "relative_floor=%s filtered=%d",
            len(candidate_scores),
            candidate_scores[-1] if candidate_scores else None,
            candidate_scores[len(candidate_scores) // 2] if candidate_scores else None,
            candidate_scores[0] if candidate_scores else None,
            score_floor,
            relative_floor,
            len(all_ranked) - len(injectable),
        )

    result = RecallResult(
        facts=injectable,
        suppressed_count=len(all_ranked) - len(injectable),
        latency_ms=(time.perf_counter() - started) * 1000.0,
        query_digest=query_digest,
    )
    # Compute the exact renderer estimate before persisting recall_log.tokens, and
    # capture WHICH facts survived truncation so record_helped() can credit only those.
    effective_budget = config.budget_tokens() if budget_tokens is None else budget_tokens
    _, surfaced_fact_ids = _render(result, budget_tokens=effective_budget)

    if run_id is not None and all_ranked:
        fact_ids = [item.fact.id for item in all_ranked]
        store.bump_access(fact_ids)
        if mode == "full":
            _strengthen_new_pairs(store, run_id, fact_ids)
        result.recall_id = store.record_recall(
            run_id=run_id,
            agent_id=agent_id,
            discipline=discipline,
            query_digest=query_digest,
            fact_ids=fact_ids,
            tokens=result.rendered_tokens,
            latency_ms=result.latency_ms,
            # Attribution is written in the SAME insert as the row; it is never updated
            # later, so nothing downstream can rewrite which facts get credited.
            surfaced_fact_ids=surfaced_fact_ids,
        )
    return result


def _safe_statement(statement: str) -> str:
    """Neutralize a fact statement before it goes into the delimited recall block.

    Collapses whitespace/control chars (forged newline entries) AND strips the literal
    recall delimiter tokens — a fact whose text contains `</recalled-knowledge>` would
    otherwise emit a spurious closing tag so a delimiter-trusting model treats trailing
    injected text as OUTSIDE the data block (council security finding).
    """
    collapsed = " ".join(statement.split())
    for token in ("<recalled-knowledge>", "</recalled-knowledge>"):
        collapsed = collapsed.replace(token, token.replace("<", "‹").replace(">", "›"))
    return collapsed


def _fact_line(item: RecalledFact) -> str:
    fact = item.fact
    prefix = "[UNVERIFIED] " if fact.status == FactStatus.QUARANTINED else ""
    if fact.capability_scope == "estate":
        from omniagentos.knowledge.capabilities import is_stale

        if is_stale(fact.last_verified):
            prefix += "[STALE—REVERIFY] "
    return (
        f"{prefix}[{fact.provenance.value}|{fact.trust:.2f}|{fact.status.value}] "
        f"{_safe_statement(fact.statement)}"
    )


def _render(result: RecallResult, *, budget_tokens: int | None = None) -> tuple[str, list[int]]:
    """Render the block AND report exactly which fact ids made the character budget.

    The surfaced list is the attribution ground truth for record_helped(): facts that
    were ranked but truncated off the tail never reached the agent and must not be
    credited when the run succeeds. Returned rather than stashed on RecallResult so this
    stays inside recall.py (the RecallResult contract is frozen).
    """
    budget = config.budget_tokens() if budget_tokens is None else budget_tokens
    char_cap = max(0, int(budget * RECALL_CHARS_PER_TOKEN))
    envelope = RECALL_HEADER + RECALL_FOOTER
    if not result.facts or len(envelope) > char_cap:
        result.rendered_tokens = 0
        return "", []

    lines: list[str] = []
    surfaced: list[int] = []
    for item in result.facts:
        line = _fact_line(item)
        candidate = RECALL_HEADER + "\n".join([*lines, line]) + "\n" + RECALL_FOOTER
        if len(candidate) > char_cap:
            break
        lines.append(line)
        surfaced.append(item.fact.id)
    if not lines:
        result.rendered_tokens = 0
        return "", []
    block = RECALL_HEADER + "\n".join(lines) + "\n" + RECALL_FOOTER
    result.rendered_tokens = int(math.ceil(len(block) / RECALL_CHARS_PER_TOKEN))
    return block, surfaced


def render_recall_block(result: RecallResult, *, budget_tokens: int | None = None) -> str:
    """Render highest-ranked whole facts within the hard character budget."""
    return _render(result, budget_tokens=budget_tokens)[0]


def _record_failure(exc: Exception, run_id: str) -> None:
    global _consecutive_failures, _last_failure_ts
    now = time.monotonic()
    with _state_lock:
        _consecutive_failures += 1
        failures = _consecutive_failures
        should_emit = (
            _last_failure_ts is None or now - _last_failure_ts >= _FAILURE_EVENT_INTERVAL_S
        )
        if should_emit:
            _last_failure_ts = now
    if not should_emit:
        return
    try:
        parsed_dsn = urlparse(config.dsn())
        safe_error = str(exc)
        # psycopg normally omits credentials, but redact defensively so a malformed
        # connection error can never copy DSN secrets into the durable H1 audit log.
        for secret in (parsed_dsn.username, parsed_dsn.password):
            if secret:
                safe_error = safe_error.replace(secret, "[REDACTED]")
        _get_audit_store().insert_event(
            "knowledge_recall_failed",
            "knowledge:recall",
            "recall_failed",
            "run",
            run_id,
            {
                "error": safe_error,
                "dsn_host": parsed_dsn.hostname,
                "consecutive_failures": failures,
            },
        )
    except Exception:
        # A secondary audit-store failure cannot be allowed to fail the run either.
        return


def _record_success() -> None:
    global _consecutive_failures, _last_failure_ts
    with _state_lock:
        _consecutive_failures = 0
        _last_failure_ts = None


def safe_recall_block(
    *, prompt: str, discipline: str | None, agent_id: str | None, run_id: str
) -> str | None:
    """Recall for a runner, including feedback, while never raising on any failure."""
    try:
        result = recall(
            _get_store(),
            prompt=prompt,
            discipline=discipline,
            agent_id=agent_id,
            run_id=run_id,
            score_floor=config.recall_score_floor(),
            floor_fraction=config.recall_floor_fraction(),
        )
        block = render_recall_block(result)
        _record_success()
        fact_count = sum(line.startswith("[") for line in block.splitlines())
        if result.recall_id is not None:
            with _state_lock:
                _recall_metadata[run_id] = {
                    "status": (
                        "injected"
                        if block
                        else "floor_suppressed"
                        if result.suppressed_count > 0
                        else "unavailable_or_empty"
                    ),
                    "recall_id": result.recall_id,
                    "fact_count": fact_count,
                    "suppressed_count": result.suppressed_count,
                }
        if not block:
            return None
        return block
    except Exception as exc:
        _record_failure(exc, run_id)
        return None


def safe_record_helped(run_id: str) -> None:
    """Record successful-run feedback without adding a finalization failure mode."""
    try:
        _get_store().record_helped(run_id)
    except Exception:
        return
