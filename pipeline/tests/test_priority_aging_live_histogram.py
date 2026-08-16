"""Read-only live-loopqueue aging histogram for the priority-aging fix."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402

LIVE_LOOPQUEUE = Path("/Users/youruser/OmniAgentOS/var/loopqueue")
TERMINAL_STATUSES = {"merged", "completed", "rejected", "closed"}


def _old_effective_priority(priority: int, created_at: str | None, *, now) -> int:
    """Pre-fix 15-minute decrement, retained only to measure the regression."""
    when = I._parse(created_at)
    if when is None or when.tzinfo is None:
        return priority
    age_s = max(0.0, (now - when).total_seconds())
    return max(0, priority - int(age_s // 900))


def _histogram(values: list[int]) -> dict[int, int]:
    counts = Counter(values)
    return {priority: counts[priority] for priority in range(4)}


def test_live_queue_effective_priority_histogram() -> None:
    queue_file = LIVE_LOOPQUEUE / "state" / "queue.json"
    if not LIVE_LOOPQUEUE.is_dir() or not queue_file.is_file():
        pytest.skip(f"live loopqueue unavailable: {LIVE_LOOPQUEUE}")

    ledger = I.LedgerView.build(LIVE_LOOPQUEUE)
    priorities = I.priorities_from_disk(LIVE_LOOPQUEUE)
    now = I._now()
    old_values: list[int] = []
    new_values: list[int] = []
    for ident, status in ledger.status.items():
        if status in TERMINAL_STATUSES:
            continue
        priority, created_at = priorities.get(ident, (I.DEFAULT_PRIORITY, None))
        old_values.append(_old_effective_priority(priority, created_at, now=now))
        new_values.append(I.effective_priority(priority, created_at, now=now))

    old_histogram = _histogram(old_values)
    new_histogram = _histogram(new_values)
    print(f"live effective_priority histogram (n={len(new_values)}): "
          f"old={old_histogram} new={new_histogram}")

    if old_values and old_histogram[0] == len(old_values):
        assert new_histogram[0] < len(new_values), (
            "the replacement formula must not remain all-zero when the old formula is"
        )
