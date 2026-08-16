"""Load the API app before any orchestrator test triggers the intake package.

``omniagentos.intake`` participates in a PRE-EXISTING, order-sensitive import cycle
(``intake.service`` -> ``api.services`` -> ``api.__init__`` -> ``api.main`` ->
``api.routes.intake`` -> ``intake.service``). It resolves cleanly whenever the ``api``
package is imported first — which the full suite already guarantees (the ``tests/api``
and ``tests/intake`` modules import the app during collection). The orchestrator loads
the intake planner lazily at run time, so importing the app here keeps these tests
robust in isolation too, without touching the out-of-ownership ``api`` package.
"""

from __future__ import annotations

import omniagentos.api.main  # noqa: F401  -- side-effect import: break the intake<->api cycle
