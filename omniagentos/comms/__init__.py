"""Comms ingestion — transcript store, inbound webhook, pollers, curated extraction.

Two-tier design (see package brief for the full contract):

- Tier 1 (always-on, zero LLM): every external message (email/Telegram/Slack/webhook)
  is normalized into the frozen message dict and stored verbatim via
  ``omniagentos.steward.store.StewardStore`` — the complete transcript, unmodified.
- Tier 2 (batch only, curated): ``omniagentos.comms.extract_batch`` selects a small,
  curated slice (VIP senders, goal keywords, or an explicit operator flag) and feeds
  it through the existing knowledge ingest pipeline as quarantined episodes. Nothing
  in this package can promote a fact; that boundary belongs to the knowledge
  subsystem's PostgreSQL role separation and consolidator.

Message bodies here are attacker-controlled. The inbound webhook path
(``omniagentos.api.routes.comms``) never touches an LLM or the knowledge subsystem;
only the offline batch path (deliberately isolated) does, and quote_untrusted delimits
the untrusted body wherever it might reach a prompt.
"""

from __future__ import annotations

__all__: list[str] = []
