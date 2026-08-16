from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_HOME_MD = Path(__file__).resolve().parents[3] / "vault" / "Home.md"


@pytest.fixture(autouse=True)
def _no_ambient_autocommit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never let a stray OMNIAGENTOS_VAULT_AUTOCOMMIT from the ambient shell
    leak into a test (contract: default OFF, and OFF in tests) — mirrors
    tests/vault/conftest.py's own guard for the H1 suite."""
    monkeypatch.delenv("OMNIAGENTOS_VAULT_AUTOCOMMIT", raising=False)
    yield


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """A bare vault_dir (no git repo), seeded with the real vault/Home.md
    skeleton so `[[Home]]` wikilink targets have something real to point at —
    every L08 note links Home, so this fixture makes "wikilinks resolve"
    testable end-to-end via the real `write_note`."""
    d = tmp_path / "vault"
    d.mkdir()
    shutil.copy(_REPO_HOME_MD, d / "Home.md")
    return d
