"""Pytest fixtures for smoke tests — each test gets an isolated tmp workspace."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.smoke.helpers import Workspace, kill_process, repo_root


@pytest.fixture
def smoke_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Workspace]:
    """Fresh DB/ledger/vault under tmp_path; chdir so relative effect paths resolve."""
    try:
        ws = Workspace.create(tmp_path)
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover
        # Only a genuinely-absent package skips. A real bug (TypeError, bad call,
        # SmokeError) must FAIL loudly, not masquerade as "system unavailable".
        pytest.skip(f"integrated system package missing for smoke workspace: {exc}")

    root = repo_root()
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = str(root) if not existing else f"{root}{os.pathsep}{existing}"

    monkeypatch.setenv("OMNIAGENTOS_DB", str(ws.db_path))
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(ws.ledger_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(ws.vault_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_AUTOCOMMIT", "false")
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    # Relative effect paths (var/smoke/...) resolve inside the tmp workspace.
    monkeypatch.chdir(ws.root)

    ws.env["OMNIAGENTOS_DB"] = str(ws.db_path)
    ws.env["OMNIAGENTOS_LEDGER_DIR"] = str(ws.ledger_dir)
    ws.env["OMNIAGENTOS_VAULT_DIR"] = str(ws.vault_dir)
    ws.env["OMNIAGENTOS_VAULT_AUTOCOMMIT"] = "false"
    ws.env["PYTHONPATH"] = pythonpath
    yield ws


@pytest.fixture
def managed_procs() -> Iterator[list]:
    """Collect Popen objects and hard-kill them after the test."""
    procs: list = []
    yield procs
    for proc in procs:
        kill_process(proc, force=True)
