"""M-20 / L-02 — classify broad handlers and dead code vs protocols."""

from __future__ import annotations

from omniagentos.testpolicy.deadcode import classify_stub
from omniagentos.testpolicy.handlers import classify_broad_handlers
from omniagentos.testpolicy.policy_load import clear_policy_cache


def setup_function() -> None:
    clear_policy_cache()


SENSITIVE_SOURCE = """
def load_summary():
    try:
        return store.fetch()
    except Exception:
        return []

def tick():
    try:
        work()
    except Exception:
        pass
"""

PROTOCOL_SOURCE = """
from typing import Protocol
from abc import ABC, abstractmethod

class Store(Protocol):
    def get(self, key: str) -> str: ...

class Base(ABC):
    @abstractmethod
    def run(self) -> None:
        raise NotImplementedError

def orphan_dead():
    pass
"""


def test_sensitive_swallowed_and_default_return_are_actionable() -> None:
    rows = classify_broad_handlers(
        SENSITIVE_SOURCE,
        relative_path="omniagentos/api/routes/reliability.py",
    )
    assert len(rows) == 2
    kinds = {r.fallback for r in rows}
    assert "default_return" in kinds
    assert "swallowed" in kinds
    assert all(r.sensitive_path for r in rows)
    assert all(r.actionable for r in rows)


def test_non_sensitive_broad_handler_is_not_actionable() -> None:
    rows = classify_broad_handlers(
        "try:\n    x()\nexcept Exception:\n    pass\n",
        relative_path="omniagentos/briefing/compose.py",
    )
    assert len(rows) == 1
    assert rows[0].fallback == "swallowed"
    assert rows[0].actionable is False
    assert "defensive" in rows[0].rationale


def test_handled_broad_handler_on_sensitive_path_not_actionable() -> None:
    src = (
        "def f():\n"
        "    try:\n"
        "        return work()\n"
        "    except Exception as exc:\n"
        "        LOG.exception('failed')\n"
        "        raise RuntimeError('degraded') from exc\n"
    )
    rows = classify_broad_handlers(src, relative_path="omniagentos/db/store.py")
    assert len(rows) == 1
    assert rows[0].fallback == "handled"
    assert rows[0].actionable is False


def test_protocol_and_abc_stubs_are_not_actionable_dead_code() -> None:
    rows = classify_stub(PROTOCOL_SOURCE, relative_path="omniagentos/example/contracts.py")
    by_name = {r.function: r for r in rows}
    assert by_name["get"].category == "protocol"
    assert by_name["run"].category == "protocol"
    assert by_name["orphan_dead"].category == "actionable_dead"


def test_intentional_stub_module_is_classified() -> None:
    src = "def run_campaign():\n    raise NotImplementedError('H4')\n"
    rows = classify_stub(src, relative_path="omniagentos/scheduler/campaign.py")
    assert len(rows) == 1
    assert rows[0].category == "intentional_stub"
