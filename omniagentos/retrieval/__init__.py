"""Shared retrieval primitives.

Today this is one thing: :mod:`omniagentos.retrieval.fusion`, the single Reciprocal
Rank Fusion implementation used by knowledge recall and file search (and registrable by
any other backend — see that module's docstring for the extension point).
"""

from __future__ import annotations

from omniagentos.retrieval.fusion import (
    RRF_K,
    BackendSpec,
    FusedEntry,
    Ranked,
    fuse,
    fuse_backends,
    fuse_ranks,
    register_backend,
    registered_backends,
    rrf_weight,
    to_ranked,
    unregister_backend,
)

__all__ = [
    "RRF_K",
    "BackendSpec",
    "FusedEntry",
    "Ranked",
    "fuse",
    "fuse_backends",
    "fuse_ranks",
    "register_backend",
    "registered_backends",
    "rrf_weight",
    "to_ranked",
    "unregister_backend",
]
