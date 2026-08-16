"""Push-side comms clients — long-lived sockets, mirroring ``comms/pollers/``.

A poller PULLS on a cursor and can always re-read a window it missed. A socket
is PUSHED to and has no cursor: the transport fires and forgets, so anything
that happens while the process is down is gone permanently. The two are
therefore not alternatives — a socket alone is strictly WORSE than the poller it
replaces, because a death at 02:00 that nobody notices until 09:00 loses seven
hours with no symptom, whereas a poller's cursor would simply have caught up.

Every client here is one half of a HYBRID and must be shipped with the other:

* the socket carries the LATENCY win (sub-second);
* the matching poller in ``comms/pollers/`` is retained as a low-frequency
  RECONCILIATION SWEEP that closes any gap automatically;
* BOTH paths write through the SAME ``normalize_* -> StewardStore`` code, and
  dedupe on ``(source, external_id)`` makes double-delivery a no-op — that is a
  SQL uniqueness constraint (``007_steward.sql``), not a convention, and it
  survives process death.

THE CURSOR RULE (the single most dangerous mistake available in this package):

    A socket client MUST NEVER write ``config["last_poll_ts"]`` on its poller's
    ``comms_sources`` row.

That value is the sweep's entire backfill window. A socket that advanced it
would erase exactly the window the sweep exists to re-read, turning the hybrid
into something strictly worse than the poller alone WITH NO VISIBLE SYMPTOM.
Each socket client therefore owns a SEPARATE source row (``<source>-socket``)
for its own liveness and counters, and shares only the message rows.
"""

from __future__ import annotations

__all__: list[str] = []
