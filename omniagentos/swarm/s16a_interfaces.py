"""S16A interface documentation only (parent L16 does not implement these).

Satellite: ``reliability/20260725/s16-interactions-routing``
Owned findings: **M-14, M-15, L-16, L-17**

Parent L16 must not duplicate the satellite's production files. After S16A
passes independent review, merge its commit into this parent and re-run the
parent nearest suites. Interfaces expected from S16A:

## M-14 — OrgDims classification visibility

* **Surface:** ``UnifiedSpawner.spawn`` OrgDims classify call site
  (parent keeps the call; S16A raises failures / low-confidence /
  unclassified results to ``LOG.warning``).
* **Do not** re-implement OrgDims warn policy in parent.

## M-15 — Decision Center / Recommended Next Action

* **Surface:** authorized grok API (``/api/grok/decision-center``,
  ``recommended-next-action``) returns capability-not-implemented (501) with
  an L20 handoff — not a fabricated product.
* **Do not** invent Decision Center UI/state in parent L16.

## L-16 — Interactions consumer + expiration

* **Surface:** ``omniagentos.interactions`` consume / answer / expire APIs and
  authorized grok routes; plain-data L10 handoff only (no scheduler edits).
* **Do not** edit ``omniagentos/swarm/scheduler.py`` for interactions.

## L-17 — Model-ranking freshness + feedback terms

* **Surface:** ``omniagentos.routing`` / longhaul ranking from the production
  modelintel registry (TTL, ISO ``updated_at``, non-inert feedback only when
  real fields present).
* **Do not** re-implement ranking in parent L16.

## L10 settle handoff (parent-owned plain data)

* **Module:** ``omniagentos.swarm.settle_handoff.settle_task_from_swarm_json``
* **Stamps on swarm_json:** ``cbm_allocation_id``, ``task_contract_id``,
  ``task_contract_hash`` (see ``REQUIRED_SWARM_JSON_STAMPS``).
* L10 commit ``454280a`` documents the reserved ``_settle_terminal`` seam and
  can receive a one-call wire-up after parent lands — without importing
  decorative helpers into scheduler ownership.
"""

from __future__ import annotations

# Documentation-only module — no runtime exports required for S16A merge.
__all__: list[str] = []
