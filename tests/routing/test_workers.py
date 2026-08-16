"""U5 worker abstraction tests."""

from __future__ import annotations

from omniagentos.routing.workers import list_terminal_workers, select_worker


def test_select_prefers_claude_when_available() -> None:
    sel = select_worker(tier="standard", effort="medium", preferred_providers=["claude", "codex"])
    assert sel.endpoint is not None
    assert sel.endpoint.provider == "claude"
    assert sel.endpoint.can_accept()


def test_catalog_nonempty() -> None:
    workers = list_terminal_workers()
    assert len(workers) >= 3
    assert all(w.mechanism == "terminal_cli" for w in workers)
