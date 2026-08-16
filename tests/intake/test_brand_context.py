"""Brand pack provision for intake scopes."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.intake.brand_context import (
    bind_project_brand,
    provision_brand_for_scope,
    try_provision_brand,
)
from omniagentos.intake.service import _resolve_working_dir
from omniagentos.projects import ProjectStore


def _pack(root: Path) -> Path:
    pack = root / "brand"
    pack.mkdir(parents=True)
    (pack / "voice.md").write_text("voice", encoding="utf-8")
    (pack / "offer.json").write_text('{"sku":"x"}', encoding="utf-8")
    (pack / "banned_claims.txt").write_text("miracle\n", encoding="utf-8")
    return pack


def test_provision(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    meta = provision_brand_for_scope(pack, scope_root=tmp_path, scope="s1")
    assert meta["banned_claims"] == ["miracle"]
    assert Path(meta["materialized"]).is_dir()


def test_try_missing() -> None:
    assert try_provision_brand(None, scope_root="/tmp", scope="x") is None


def test_enforce_binds_pack_from_project_registry_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
    collab = CollabStore(str(tmp_path / "brand.db"))
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = _pack(project_root)
    ProjectStore(collab._store).create_project(
        {"id": "proj_brand", "name": "Brand", "root_dirs": [str(project_root)]}
    )

    contract = bind_project_brand(
        collab._store,
        project_id="proj_brand",
        working_dir=project_root,
        scope="proj_brand",
    )

    assert contract is not None
    assert contract.brand is not None
    assert contract.brand.path == source.resolve()
    materialized = project_root / "var" / "inputs" / "proj_brand" / "brand"
    assert (materialized / "voice.md").read_text(encoding="utf-8") == "voice"


def test_shadow_resolves_project_brand_without_materializing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "shadow")
    collab = CollabStore(str(tmp_path / "shadow.db"))
    project_root = tmp_path / "project"
    project_root.mkdir()
    _pack(project_root)
    ProjectStore(collab._store).create_project(
        {"id": "proj_brand", "name": "Brand", "root_dirs": [str(project_root)]}
    )

    contract = bind_project_brand(
        collab._store,
        project_id=None,
        working_dir=project_root,
        scope="proj_brand",
    )

    assert contract is not None
    assert contract.brand is not None
    assert not (project_root / "var" / "inputs").exists()


def test_global_brand_pack_is_ignored_for_unscoped_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    global_pack_root = tmp_path / "global"
    global_pack_root.mkdir()
    _pack(global_pack_root)
    monkeypatch.setenv("OMNIAGENTOS_BRAND_PACK", str(global_pack_root / "brand"))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
    collab = CollabStore(str(tmp_path / "scratch.db"))

    scratch = Path(_resolve_working_dir(collab._store, "btk_scratch", None))

    assert scratch.is_dir()
    assert not (scratch / "var" / "inputs").exists()
