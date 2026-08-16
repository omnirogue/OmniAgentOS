"""Executive Decision Center (EDC).

A generic, owner-scoped decision pipeline: source event → classify → Decision
(with a REQUIRED recommended action) → resolve → outcome verification →
learning. Email is source adapter #1; the adapter boundary
(:mod:`omniagentos.edc.adapters.base`) is the entire future-sources surface.

P0 (this package's first slice) ships the substrate only: the owner-scoped
:class:`~omniagentos.edc.store.DecisionStore` over migration 130, the source
account → owner map (:mod:`omniagentos.edc.accounts`), and the source-adapter
Protocol. Classification, resolution, Slack, and the API arrive in later phases.
"""

from __future__ import annotations

__all__: list[str] = []
