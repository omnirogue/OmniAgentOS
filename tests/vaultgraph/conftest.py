from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.vaultgraph import VaultGraph, build_graph

FIXTURE_VAULT = Path(__file__).resolve().parent / "fixture_vault"


@pytest.fixture
def fixture_vault() -> Path:
    """Read-only path to the checked-in tiny fixture vault."""
    return FIXTURE_VAULT


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """A writable copy of the fixture vault (for MOC generation tests)."""
    dest = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, dest)
    return dest


@pytest.fixture
def graph(fixture_vault: Path) -> Iterator[VaultGraph]:
    g = build_graph(fixture_vault)
    try:
        yield g
    finally:
        g.close()
