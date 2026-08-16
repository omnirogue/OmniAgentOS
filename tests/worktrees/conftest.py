"""Let the mechanism tests exercise per-unit worktrees despite the interlock.

``lanes._EXECUTOR_WIRING_COMPLETE`` is False in production, so
``lane_worktrees_enabled`` returns False no matter what the env or config says.
That is deliberate (see the constant's comment: with the flag on today, a lane
would create and merge a worktree while its EXECUTOR still ran in the shared
project directory -- claims and writes in different places, which is worse than
today's serialization).

The MECHANISM is nonetheless complete and worth testing now, so this fixture
lifts the interlock for this package only. It is an explicit, greppable
monkeypatch rather than an env escape hatch precisely because an env hatch is
something a person could reasonably set on a real host; a test-only attribute
patch is not.

``test_interlock_holds_without_this_fixture`` opts OUT and asserts the production
behaviour, so the interlock itself cannot regress unnoticed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omniagentos.worktrees import lanes


@pytest.fixture(autouse=True)
def _lift_executor_wiring_interlock(request: pytest.FixtureRequest) -> Iterator[None]:
    if "no_worktree_interlock_lift" in request.keywords:
        # Must still yield: this is a generator fixture, and a bare return makes
        # it a generator that never yields, which pytest rejects outright.
        yield
        return
    original = lanes._EXECUTOR_WIRING_COMPLETE
    lanes._EXECUTOR_WIRING_COMPLETE = True
    try:
        yield
    finally:
        lanes._EXECUTOR_WIRING_COMPLETE = original
