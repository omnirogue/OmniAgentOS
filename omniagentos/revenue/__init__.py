"""Dimensional revenue / P&L collection and storage.

This package owns the ``revenue_facts`` dimensional store (vertical + source +
campaign), the multi-brand collector that populates it, and the read model the
``/api/revenue`` endpoint serves. It is deliberately separate from
``omniagentos.goals`` — the legacy scalar ``metric_snapshots`` collector there is
left working unchanged.
"""
