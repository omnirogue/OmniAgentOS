"""Materialize brand packs into intake/session inputs (TN.11)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from omniagentos.brandpacks import BrandPackError, load_brand_pack, materialize_to_inputs
from omniagentos.brandpacks.pack import (
    ProjectContract,
    project_contract_mode,
    resolve_project_contract,
)

LOG = logging.getLogger(__name__)


def provision_brand_for_scope(
    brand_pack_path: str | Path,
    *,
    scope_root: str | Path,
    scope: str,
) -> dict[str, Any]:
    """Load pack and copy into scope inputs. Returns paths + fingerprint metadata."""
    pack = load_brand_pack(brand_pack_path)
    dest = materialize_to_inputs(pack, scope_root, scope=scope)
    return {
        "brand_root": str(pack.path),
        "materialized": str(dest),
        "banned_claims": list(pack.banned_claims),
        "offer_keys": sorted(pack.offer.keys()),
        "voice_chars": len(pack.voice_md),
    }


def try_provision_brand(
    brand_pack_path: str | Path | None,
    *,
    scope_root: str | Path,
    scope: str,
) -> dict[str, Any] | None:
    """Best-effort; returns None if path missing or invalid (never raises for intake)."""
    if not brand_pack_path:
        return None
    try:
        return provision_brand_for_scope(brand_pack_path, scope_root=scope_root, scope=scope)
    except (BrandPackError, OSError, ValueError):
        return None


def bind_project_brand(
    store: Any,
    *,
    project_id: str | None,
    working_dir: str | Path,
    scope: str,
) -> ProjectContract | None:
    """Resolve and optionally materialize the registered project's brand pack.

    ``off`` performs no registry lookup. ``shadow`` resolves and logs the pack
    that would be bound without writing inputs. ``enforce`` materializes only
    that project's pack under its own working root.
    """
    mode = project_contract_mode()
    if mode == "off":
        return None
    contract = resolve_project_contract(
        store,
        project_id=project_id,
        working_dir=working_dir,
    )
    if contract is None:
        return None
    if mode == "shadow":
        LOG.info(
            "project brand shadow diff project=%s pack=%s",
            contract.project.get("id"),
            contract.brand.path if contract.brand is not None else None,
        )
        return contract
    if contract.brand is not None:
        try_provision_brand(
            contract.brand.path,
            scope_root=working_dir,
            scope=scope,
        )
    return contract
