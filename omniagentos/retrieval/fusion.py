"""Reciprocal Rank Fusion — the ONE implementation every retrieval backend shares.

WHY RANK FUSION AND NOT SCORE FUSION
------------------------------------
Retrieval backends produce scores on mutually incompatible scales: SQLite/Postgres FTS
returns a BM25-ish relevance rank, pgvector returns cosine similarity in [-1, 1], the
knowledge graph returns a spreading-activation weight, Spotlight returns nothing but an
ordering, and a LIKE-scorer returns integer term counts. Normalizing those against each
other requires per-backend calibration that drifts the moment either backend changes.
Reciprocal Rank Fusion sidesteps the problem entirely: it throws the scores away and
uses only *rank position*, which is uniformly comparable across every backend. A
document surfaced by two independent backends outranks one surfaced strongly by a
single backend — exactly the property hybrid retrieval is bought for.

    score(d) = sum over backends b that surfaced d of  weight(b) / (k + rank_b(d))

``k`` (default 60, the value from Cormack et al. 2009 and the de-facto standard) damps
the influence of the very top positions so a single backend's #1 cannot dominate a
result agreed on by several backends.

TWO ENTRY POINTS, ONE ARITHMETIC
--------------------------------
* :func:`fuse` — the list-oriented form. Give it ``{backend_name: ranked_list}`` and it
  returns fused entries carrying a per-backend ``contributions`` map for telemetry.
  Build the ranked lists with :func:`to_ranked`.
* :func:`fuse_ranks` — the row-oriented form, for backends that already emit rank
  *columns* per candidate (the knowledge store's one-round-trip candidate query returns
  ``vector_rank``/``fts_rank`` alongside each row, so there is no list to walk).

Both delegate to :func:`rrf_weight`, so there is exactly one place where the RRF
arithmetic lives.

EXTENSION POINT — adding a backend without touching any caller
--------------------------------------------------------------
Backends register themselves as :class:`BackendSpec` values::

    from omniagentos.retrieval import fusion

    fusion.register_backend(fusion.BackendSpec(
        name="repomap",
        search=lambda query, limit: repomap_service.rank(query, limit),
        id_fn=lambda hit: hit.path,
    ))

and a caller then fuses every registered backend at once::

    entries = fusion.fuse_backends("payment retry", limit=20)

``fuse_backends`` accepts ``names=`` to select a subset, so a caller can opt into the
backends that make sense for it. Registration is process-local and idempotent by name;
call it from the owning module's import path (or from wiring code) so that adding
repomap, the filesearch catalog, vaultgraph, skills, or a future artifact corpus to the
fused result needs no edit to ``knowledge/recall.py`` or ``filesearch/semantic.py``.

Per-backend ``weight`` exists for the case where one backend is known to be
systematically more precise than another; it defaults to 1.0, which is plain RRF.

RANK ORIGIN: ``rank`` is whatever the caller supplies. :func:`to_ranked` defaults to
1-based ranks; pass ``start=0`` for a backend whose established behavior is 0-based.
The origin shifts all of that backend's weights uniformly, so it only matters when
matching a pre-existing scoring convention exactly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

__all__ = [
    "RRF_K",
    "BackendSpec",
    "FusedEntry",
    "Ranked",
    "fuse",
    "fuse_backends",
    "fuse_ranks",
    "registered_backends",
    "register_backend",
    "rrf_weight",
    "to_ranked",
    "unregister_backend",
]

RRF_K = 60
"""The standard reciprocal-rank-fusion damping constant."""


@dataclass(frozen=True, slots=True)
class Ranked:
    """One backend's ``(id, rank, payload)`` triple. ``rank`` origin is the caller's."""

    id: str
    rank: int
    payload: Any = None


@dataclass(slots=True)
class FusedEntry:
    """A fused document: its RRF score plus the per-backend evidence behind it."""

    id: str
    score: float
    contributions: dict[str, int] = field(default_factory=dict)
    """backend name -> the rank that backend gave this id (telemetry + explainability)."""
    payload: Any = None
    """Payload of the FIRST backend that surfaced this id (backend iteration order)."""
    payloads: dict[str, Any] = field(default_factory=dict)
    """Every backend's payload for this id, so a caller can prefer a specific leg."""

    @property
    def backend_count(self) -> int:
        """How many independent backends surfaced this id."""
        return len(self.contributions)

    def rank_signals(self) -> dict[str, float]:
        """``{backend: rank}`` as floats — the telemetry shape recall signals use."""
        return {name: float(rank) for name, rank in self.contributions.items()}


def rrf_weight(rank: int, k: int = RRF_K, weight: float = 1.0) -> float:
    """The RRF contribution of a single ranked position. The ONLY place this is computed."""
    return weight / (k + rank)


def to_ranked(
    items: Iterable[Any],
    id_fn: Callable[[Any], str],
    *,
    start: int = 1,
    payload_fn: Callable[[Any], Any] | None = None,
    dedupe: bool = True,
) -> list[Ranked]:
    """Turn a backend's ordered results into ``Ranked`` triples.

    ``id_fn`` produces the cross-backend identity (a realpath, a fact id, a symbol
    key) — two backends must agree on it or their evidence cannot be combined.

    ``dedupe`` (default) collapses a repeated id to its BEST (first) rank, so one
    backend returning the same document twice cannot double-dip its own vote. Pass
    ``dedupe=False`` to keep every occurrence; :func:`fuse` then sums all of them while
    still reporting the best rank in ``contributions``.
    """
    ranked: list[Ranked] = []
    seen: set[str] = set()
    for offset, item in enumerate(items):
        key = id_fn(item)
        if dedupe:
            if key in seen:
                continue
            seen.add(key)
        ranked.append(Ranked(key, start + offset, item if payload_fn is None else payload_fn(item)))
    return ranked


def fuse(
    backends: Mapping[str, Sequence[Ranked] | Iterable[Ranked]],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[FusedEntry]:
    """Fuse ``{backend_name: ranked_list}`` into one descending-score ranking.

    Ties keep first-appearance order (backend iteration order, then rank order), and
    ``entry.payload`` is the first backend's payload for that id — so a caller whose
    leading backend carries the richer payload (a snippet, say) gets it for free by
    listing that backend first.
    """
    fused: dict[str, FusedEntry] = {}
    for name, ranked in backends.items():
        weight = 1.0 if weights is None else float(weights.get(name, 1.0))
        for item in ranked:
            entry = fused.get(item.id)
            if entry is None:
                entry = FusedEntry(id=item.id, score=0.0, payload=item.payload)
                fused[item.id] = entry
            entry.score += rrf_weight(item.rank, k, weight)
            # setdefault: a leg that lists an id twice still reports its BEST rank.
            entry.contributions.setdefault(name, item.rank)
            entry.payloads.setdefault(name, item.payload)
    # Incremental ``+=`` (not ``sum``) on purpose: ids stream in one leg at a time, and
    # this is bit-for-bit the accumulation the file-search fusion has always used.
    return sorted(fused.values(), key=lambda entry: entry.score, reverse=True)


def fuse_ranks(
    ranks: Mapping[str, int | None],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
    entry_id: str = "",
) -> FusedEntry | None:
    """Row-oriented RRF: fuse one candidate's per-backend rank columns.

    ``None`` ranks mean "that backend did not surface this candidate" and are dropped.
    Returns ``None`` when no backend surfaced it at all (nothing to fuse).
    """
    entry = FusedEntry(id=entry_id, score=0.0)
    for name, rank in ranks.items():
        if rank is not None:
            entry.contributions[name] = int(rank)
    if not entry.contributions:
        return None
    # One ``sum`` over the whole row (CPython's compensated float summation) rather than
    # incremental ``+=``: more accurate, and bit-for-bit what knowledge recall has always
    # computed. All legs for a row are known up front here, so nothing streams.
    entry.score = sum(
        rrf_weight(rank, k, 1.0 if weights is None else float(weights.get(name, 1.0)))
        for name, rank in entry.contributions.items()
    )
    return entry


# ---------------------------------------------------------------------------
# Backend registry — the extension point (see module docstring).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """A registrable retrieval leg.

    ``search(query, limit)`` returns that backend's results already in its own best-first
    order; ``id_fn`` maps one result to the cross-backend identity. ``start`` is the
    rank origin, ``weight`` scales this leg's RRF contribution (1.0 = plain RRF).
    """

    name: str
    search: Callable[[str, int], Iterable[Any]]
    id_fn: Callable[[Any], str]
    start: int = 1
    weight: float = 1.0
    payload_fn: Callable[[Any], Any] | None = None


_registry_lock = RLock()
_registry: dict[str, BackendSpec] = {}


def register_backend(spec: BackendSpec) -> None:
    """Register (or replace) a retrieval leg by name. Idempotent, process-local."""
    with _registry_lock:
        _registry[spec.name] = spec


def unregister_backend(name: str) -> None:
    """Drop a registered leg. Missing names are ignored (test-teardown friendly)."""
    with _registry_lock:
        _registry.pop(name, None)


def registered_backends() -> dict[str, BackendSpec]:
    """A snapshot of the registry, in registration order."""
    with _registry_lock:
        return dict(_registry)


def fuse_backends(
    query: str,
    *,
    limit: int = 20,
    names: Sequence[str] | None = None,
    k: int = RRF_K,
    per_backend_limit: int | None = None,
) -> list[FusedEntry]:
    """Run the registered backends (or the ``names`` subset) and fuse their results.

    A backend that raises is skipped — one dead leg degrades the fusion, it never fails
    the query. Over-fetching each leg (``per_backend_limit``, default ``2 * limit``) is
    what gives fusion something to disagree about.
    """
    specs = registered_backends()
    selected = [specs[name] for name in names if name in specs] if names else list(specs.values())
    fetch = per_backend_limit if per_backend_limit is not None else limit * 2
    ranked_legs: dict[str, list[Ranked]] = {}
    weights: dict[str, float] = {}
    for spec in selected:
        try:
            results = spec.search(query, fetch)
        except Exception:  # noqa: BLE001 -- one dead leg must not fail the whole query
            continue
        ranked_legs[spec.name] = to_ranked(
            results, spec.id_fn, start=spec.start, payload_fn=spec.payload_fn
        )
        weights[spec.name] = spec.weight
    return fuse(ranked_legs, k=k, weights=weights)[:limit]
