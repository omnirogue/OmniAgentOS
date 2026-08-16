"""Isolated runtime-root fixtures for provision tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.connectors import load_registry


@pytest.fixture
def provision_var_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Provide a complete function-isolated var root and connector registry."""
    root = tmp_path / "var"
    root.mkdir()
    source = Path(__file__).resolve().parents[2] / "configs" / "connectors.yaml"
    (root / "connectors.yaml").write_bytes(source.read_bytes())
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(root))
    load_registry.cache_clear()
    try:
        yield root
    finally:
        load_registry.cache_clear()
