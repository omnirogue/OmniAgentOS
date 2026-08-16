"""Direct policy checks for malformed simulation contexts."""

from __future__ import annotations

import pytest

from omniagentos.policy import dir_grants
from omniagentos.simgate import SimContext


def test_allowed_grant_roots_rejects_sim_context_without_campaign_root() -> None:
    context = SimContext(sim_mode=True, campaign="x", campaign_root=None)

    with pytest.raises(dir_grants.DirGrantError, match="broken invariant") as excinfo:
        dir_grants.allowed_grant_roots(sim_ctx=context)
    assert "operator error" not in str(excinfo.value)
