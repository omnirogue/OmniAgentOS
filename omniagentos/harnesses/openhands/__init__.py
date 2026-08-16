"""OpenHands SDK harness package (harness id ``openhands``).

Import `omniagentos.harnesses.openhands.adapter` for `OpenHandsAdapter`
(contracts/interfaces.md Section "p04 -- omniagentos.adapters"). This
`__init__.py` intentionally imports nothing from `adapter` or `openhands`
itself, so `import omniagentos.harnesses.openhands` is always cheap and never
touches the (optional) `openhands-sdk` package.
"""

from __future__ import annotations
