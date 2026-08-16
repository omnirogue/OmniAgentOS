from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.vault.helpers import git

_REPO_HOME_MD = Path(__file__).resolve().parents[2] / "vault" / "Home.md"


@pytest.fixture(autouse=True)
def _no_ambient_autocommit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never let a stray OMNIAGENTOS_VAULT_AUTOCOMMIT from the ambient shell
    leak into a test (contract: default OFF, and OFF in tests). Tests that
    want autocommit ON opt in explicitly, per-test."""
    monkeypatch.delenv("OMNIAGENTOS_VAULT_AUTOCOMMIT", raising=False)
    yield


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """A bare vault_dir (no git repo), seeded with the real vault/Home.md
    skeleton so update_home / [[Home]] wikilink targets have something real
    to point at."""
    d = tmp_path / "vault"
    d.mkdir()
    shutil.copy(_REPO_HOME_MD, d / "Home.md")
    return d


@pytest.fixture
def git_vault_dir(tmp_path: Path) -> Path:
    """A vault_dir inside a fresh git repo with one seed commit (Home.md), for
    auto-commit tests. Returns the vault/ subdirectory; the repo root is its
    parent."""
    repo = tmp_path / "repo"
    d = repo / "vault"
    d.mkdir(parents=True)
    shutil.copy(_REPO_HOME_MD, d / "Home.md")
    git(repo, "init", "-q")
    git(repo, "add", "vault/Home.md")
    git(
        repo,
        "-c",
        "user.name=seed",
        "-c",
        "user.email=seed@example.com",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    return d
