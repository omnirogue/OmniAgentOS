"""The autonomy lease subsystem implements a sandboxed, leased execution container for agents.

It is dark by default behind the ``OMNIAGENTOS_AUTONOMY_LEASE`` feature flag, allowing
the core framework to reference lease mechanisms safely before activation.
"""

from __future__ import annotations
