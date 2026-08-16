"""CORAL enforce mode — hub provisioning + inline rendering under tiny byte caps.

Runs entirely in-process against tmp directories: ``coral_hub_references``
(discover + provision) and ``UnifiedSpawner._coral_fallback_excerpt`` (the
reference renderer) are exercised directly, so NO provider process is ever
spawned.

Precondition note: ``collision_safety.swarm_worktrees_enforceable`` gates only
``swarm_worktrees_enabled()`` (the config-driven worktree-isolation flip in
worktrees.py) — the CORAL provisioning path itself (`provision_coral_hub` /
`coral_hub_references`) has no such predicate, so no monkeypatch of that gate
is needed for these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.swarm.spawn import (
    CORAL_FALLBACK_HARD_MAX_TOTAL_BYTES,
    CORAL_INLINE_TOTAL_BYTES_ENV,
    UnifiedSpawner,
    coral_inline_budget,
)
from omniagentos.swarm.worktrees import (
    CORAL_CONTEXT_ENV,
    CORAL_HUB_DIR,
    coral_context_mode,
    coral_hub_references,
)

TINY_TOTAL = 64


@pytest.fixture
def shared_root(tmp_path: Path) -> Path:
    """Populated approved CORAL shared root (skills/playbooks/runs)."""
    root = tmp_path / "coral-shared"
    (root / "skills").mkdir(parents=True)
    (root / "playbooks").mkdir()
    (root / "runs").mkdir()
    (root / "skills" / "alpha.md").write_text("SKILL_ALPHA " + "a" * 500, encoding="utf-8")
    (root / "skills" / "beta.md").write_text("SKILL_BETA " + "b" * 500, encoding="utf-8")
    (root / "playbooks" / "pb.md").write_text("PLAYBOOK_PB " + "p" * 300, encoding="utf-8")
    (root / "runs" / "note.md").write_text("RUN_NOTE " + "r" * 300, encoding="utf-8")
    return root


@pytest.fixture
def worker(tmp_path: Path) -> Path:
    directory = tmp_path / "worker"
    directory.mkdir()
    return directory


def test_enforce_provisions_worker_local_hub(
    shared_root: Path, worker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CORAL_CONTEXT_ENV, "enforce")
    monkeypatch.setenv(CORAL_INLINE_TOTAL_BYTES_ENV, str(TINY_TOTAL))
    assert coral_context_mode() == "enforce"

    references = coral_hub_references(worker, shared_root)

    # Every populated category is exposed through the worker-local hub.
    assert {r.kind for r in references} == {"skills", "playbooks", "runs"}
    assert len(references) == 4
    for reference in references:
        link = worker / reference.worker_path
        assert reference.worker_path.startswith(CORAL_HUB_DIR)
        assert link.is_symlink(), f"{link} must be a hub symlink in enforce mode"
        resolved = link.resolve()
        assert resolved.is_file()
        # Containment: every provisioned link resolves under the approved root.
        assert resolved.is_relative_to(shared_root.resolve())
        assert reference.size_bytes == resolved.stat().st_size
    # The hub is gitignored so `git add -A` in a worker can never stage it.
    assert (worker / CORAL_HUB_DIR / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_tiny_inline_budget_env_is_honored_and_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CORAL_INLINE_TOTAL_BYTES_ENV, str(TINY_TOTAL))
    total, per_reference = coral_inline_budget()
    assert total == TINY_TOTAL
    # per-reference cap can never advertise more than the total can fund.
    assert per_reference <= total

    # An absurd value is clamped to the hard max, an invalid one falls back.
    monkeypatch.setenv(CORAL_INLINE_TOTAL_BYTES_ENV, "999999999")
    assert coral_inline_budget()[0] == CORAL_FALLBACK_HARD_MAX_TOTAL_BYTES


def test_reference_rendering_respects_tiny_byte_caps(
    shared_root: Path, worker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CORAL_CONTEXT_ENV, "enforce")
    monkeypatch.setenv(CORAL_INLINE_TOTAL_BYTES_ENV, str(TINY_TOTAL))

    references = coral_hub_references(worker, shared_root)
    total_cap, per_reference_cap = coral_inline_budget()
    assert total_cap == TINY_TOTAL

    excerpt = UnifiedSpawner._coral_fallback_excerpt(
        references=references,
        hits=[],
        registry_rows=[],
        total_cap=total_cap,
        per_reference_cap=per_reference_cap,
    )

    inlined = [item.inlined_bytes for item in excerpt.per_reference]
    # Content bytes never exceed the tiny total, and no single reference
    # exceeds the (total-clamped) per-reference ceiling.
    assert sum(inlined) <= total_cap
    assert all(size <= per_reference_cap for size in inlined)
    # The budget is tiny while the hub holds ~1.6 KiB, so rendering must
    # truncate and/or drop — but must say so honestly, never silently.
    assert excerpt.truncated or excerpt.dropped > 0
    if excerpt.truncated:
        assert "TRUNCATED" in excerpt.text
