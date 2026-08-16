"""Regression coverage for connector-registry campaign isolation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import omniagentos.connectors as connectors


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> Iterator[None]:
    connectors.load_registry.cache_clear()
    yield
    connectors.load_registry.cache_clear()


def _registry_yaml(label: str) -> str:
    return f"""\
version: 1
groups:
  test:
    label: Test
connectors:
  sentinel:
    label: {label}
    group: test
    capabilities:
      sentinel.read:
        label: Read sentinel
        action_class: read_only
"""


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_var_override_loads_campaign_registry_without_touching_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign-var"
    campaign.mkdir()
    expected = campaign / "connectors.yaml"
    expected.write_text(_registry_yaml("Campaign registry"), encoding="utf-8")

    # Both are deliberate counterfeits.  A retained __file__-relative lookup or
    # an invented <var>/configs layout will load one of these instead.
    checkout = tmp_path / "operator-checkout"
    fake_module = checkout / "omniagentos" / "connectors" / "__init__.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# operator source sentinel\n", encoding="utf-8")
    source_registry = checkout / "configs" / "connectors.yaml"
    source_registry.parent.mkdir(parents=True)
    source_registry.write_text(_registry_yaml("Operator registry"), encoding="utf-8")
    alternate = campaign / "configs" / "connectors.yaml"
    alternate.parent.mkdir()
    alternate.write_text(_registry_yaml("Alternate registry"), encoding="utf-8")
    checkout_before = _tree_bytes(checkout)

    monkeypatch.setattr(connectors, "__file__", str(fake_module))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(campaign))
    connectors.load_registry.cache_clear()

    registry = connectors.load_registry()

    assert connectors._default_registry_path() == expected
    assert registry.connectors["sentinel"].label == "Campaign registry"
    assert _tree_bytes(checkout) == checkout_before


@pytest.mark.parametrize("configured", ["", "   "])
def test_blank_var_override_keeps_checkout_registry(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", configured)

    expected = Path(connectors.__file__).resolve().parents[2] / "configs" / "connectors.yaml"
    assert connectors._default_registry_path() == expected
