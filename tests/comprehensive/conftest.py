"""Shared fixtures and scale knobs for the comprehensive suite."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore
from omniagentos.orgdims.service import OrgDimsService


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@pytest.fixture(scope="module")
def workers() -> int:
    """Default thread pool size for parallel stress."""
    return _int_env("COMPREHENSIVE_WORKERS", 24)


@pytest.fixture(scope="module")
def diamond_count() -> int:
    return _int_env("COMPREHENSIVE_DIAMONDS", 32)


@pytest.fixture()
def product_db(tmp_path: Path) -> str:
    return str(tmp_path / "comprehensive.db")


@pytest.fixture()
def graph_svc(product_db: str) -> GraphRuntimeService:
    return GraphRuntimeService(db_path=product_db)


@pytest.fixture()
def cbm_svc(product_db: str) -> CognitiveBudgetService:
    return CognitiveBudgetService(database=product_db)


@pytest.fixture()
def orgdims_svc(product_db: str) -> OrgDimsService:
    svc = OrgDimsService(db_path=product_db)
    svc.ensure_seeded()
    return svc


@pytest.fixture()
def metacog_svc(tmp_path: Path, product_db: str, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()
    store = MetacogStore(product_db)
    return MetacogService(store=store)


@pytest.fixture()
def pool(workers: int):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        yield ex
