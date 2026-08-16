"""Team Work OS — the human half of the shared board (migration 123).

``omniagentos.collab`` owns the board itself: cards, claims, agent messaging.
This package owns what a TEAM needs on top of it — the append-only evidence
spine, the per-card audit trail, verification, per-person queues, and the daily
productivity snapshot.

The split is deliberate. Collab is frozen infrastructure every agent path
writes through; the rules here (a card is not done until evidence exists, a
person does not verify their own work) bind team cards only and must never
change what an agent card can do.
"""
