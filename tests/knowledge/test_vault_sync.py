"""Admin vault synchronization is idempotent and conservative."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.knowledge.contracts import FactStatus
from omniagentos.knowledge.extract import FixtureExtractor
from omniagentos.knowledge.vault_sync import ingest_vault, sync_path


def test_vault_sync_is_idempotent_and_high_trust(
    knowledge_store_admin: object, monkeypatch: object
) -> None:
    store = knowledge_store_admin

    # The vault helper delegates extraction; pin it for a hermetic idempotency test.
    import omniagentos.knowledge.vault_sync as vault_sync

    original = vault_sync.ingest_episode
    monkeypatch.setattr(
        vault_sync,
        "ingest_episode",
        lambda *args, **kwargs: original(*args, extractor=FixtureExtractor(), **kwargs),
    )
    content = "FACT: Vault notes are administrator-synced."
    first = ingest_vault(store, note_id="notes/one.md", title="One", content=content)
    second = ingest_vault(store, note_id="notes/one.md", title="One", content=content)

    assert second.episode_id == first.episode_id
    assert second.fact_ids == first.fact_ids
    fact = store.get_fact(first.fact_ids[0])
    assert fact is not None
    assert fact.trust == 0.9
    assert fact.status is FactStatus.QUARANTINED
    assert store._role == "knowledge_admin"


def test_sync_path_missing_path_is_not_empty_success(tmp_path: Path) -> None:
    """A missing vault path must not look like a successful empty sync.

    Counterfeits that must still fail:
    - return [] for missing paths (operator/CLI prints 'synced 0' and exit 0)
    - only special-case path is None / empty string
    - raise only for files, still treat missing directories as empty rglob
    """
    missing = tmp_path / "does-not-exist" / "vault"
    assert not missing.exists()
    with pytest.raises(FileNotFoundError, match="does not exist|not found|missing"):
        sync_path(object(), missing)  # type: ignore[arg-type]


def test_sync_path_empty_directory_is_genuine_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing directory with no markdown is a real empty result, not an error."""
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    # Must not call ingest when there is nothing to sync.
    import omniagentos.knowledge.vault_sync as vault_sync

    monkeypatch.setattr(
        vault_sync,
        "ingest_vault",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not ingest")),
    )
    assert sync_path(object(), empty) == []  # type: ignore[arg-type]
