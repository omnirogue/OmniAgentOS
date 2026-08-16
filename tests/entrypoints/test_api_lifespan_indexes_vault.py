"""The real API startup syncs the repo playbook into the runtime vault and indexes it.

Entry point: the real ASGI app's ``lifespan`` (``omniagentos.api:app``),
which is what ``make api`` / ``uvicorn omniagentos.api:app`` runs.

With ``OMNIAGENTOS_INDEX_VAULT_ON_STARTUP=1`` and ``OMNIAGENTOS_VAULT_DIR``
pointed at an empty tmp vault, startup must (a) mirror the repo's
``vault/playbook`` notes into the runtime vault (a copy-only, never-deletes
mirror — see ``sync_playbook_from_repo``) and (b) index whatever notes land
there via ``index_vault_playbook``, so ``GET /api/skills/tree`` serves at
least the six 032 migration stubs even when the repo playbook itself is
empty. This checkout's ``vault/playbook`` ships no notes beyond
``.gitkeep``, so the six migration-seeded skills are the whole library here;
assertions stay loose on the total count on purpose so a richer playbook
elsewhere is not treated as a regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_api_startup_syncs_and_indexes_vault_playbook(
    campaign_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_vault = tmp_path / "runtime-vault"
    runtime_vault.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(runtime_vault))
    monkeypatch.setenv("OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_INDEX_VAULT_ON_STARTUP", "1")

    # Import the production app object — do not hand-assemble a FastAPI().
    from omniagentos.api import app as production_app

    # Entering the context runs the real lifespan startup hook.
    with TestClient(production_app) as client:
        # The mirror ran: the runtime vault now has a playbook directory (even
        # though this checkout's repo playbook itself carries no notes).
        assert (runtime_vault / "playbook").is_dir()

        response = client.get("/api/skills/tree")
        assert response.status_code == 200
        tree = response.json()

    slugs = [
        str(skill["slug"])
        for category in tree
        for subcategory in category["subcategories"]
        for skill in subcategory["skills"]
    ]
    assert "webinars_script" in slugs
    assert len(slugs) >= 6
