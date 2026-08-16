"""The real API startup mints the session token on a fresh tree (FIX 3).

Entry point: the real ASGI app's ``lifespan`` (``omniagentos.api:app``),
which is what ``make api`` / ``uvicorn omniagentos.api:app`` runs.

``sessions/token.py`` split verify from mint: a missing token file is now an
authentication FAILURE (``verify_token`` reads only), never a mint event. The
only other minter, ``sessions/hook_client.py``, runs from INSIDE an
already-spawned session -- which nothing can spawn without a token that
exists. Without a first-boot mint, a brand-new var root can never produce its
first session token at all.

Mechanism observed through the public API: after startup with
``OMNIAGENTOS_MINT_TOKEN_ON_STARTUP=1`` (the production default), the token
file exists and a request presenting it as ``X-Session-Token`` is accepted
by the app-level gate. A separate test proves startup never re-mints over an
already-existing token. The session-wide conftest pin keeps this flag off
everywhere else.

Each test enters ``TestClient(production_app)`` exactly once and turns the
orch-resume daemon back off (``campaign_root`` unconditionally enables it
with a 0s startup delay for its OWN tests' benefit; this file does not
exercise orch-resume, and that daemon thread outliving its `with
TestClient(...)` block is a latent cross-test race against the *next* test's
monkeypatched ``OMNIAGENTOS_DB`` / cache-cleared ``get_store()`` that has
nothing to do with token minting).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_api_startup_mints_token_on_a_fresh_tree(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real lifespan must mint the token exactly once on a brand-new var root."""
    monkeypatch.setenv("OMNIAGENTOS_MINT_TOKEN_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")

    from omniagentos.sessions import token as token_mod

    token_path = token_mod.token_path()
    assert not token_path.exists(), "test fixture must start with no token file"

    # Import the production app object — do not hand-assemble a FastAPI().
    from omniagentos.api import app as production_app

    # Entering the context runs the real lifespan startup hook.
    with TestClient(production_app) as client:
        assert token_path.is_file()
        minted = token_path.read_text(encoding="utf-8").strip()
        assert minted

        # A real, unauthenticated-except-for-the-token request must now be
        # accepted by the app-level gate (proves the minted token is the
        # ACTIVE one, not merely a file that happens to exist).
        resp = client.post(
            "/api/routines",
            json={"name": "probe-mint"},
            headers={"X-Session-Token": minted},
        )
        assert resp.status_code != 401, resp.text


def test_api_startup_never_remints_a_preexisting_token(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed the other direction: an existing token must survive startup untouched."""
    monkeypatch.setenv("OMNIAGENTOS_MINT_TOKEN_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")

    from omniagentos.sessions import token as token_mod

    token_path = token_mod.token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("pre-existing-token-value\n", encoding="utf-8")

    from omniagentos.api import app as production_app

    with TestClient(production_app):
        assert token_path.read_text(encoding="utf-8").strip() == "pre-existing-token-value"


def test_api_startup_mint_disabled_leaves_fresh_tree_tokenless(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-out must actually opt out (mirrors the seed/index/resume flags)."""
    monkeypatch.setenv("OMNIAGENTOS_MINT_TOKEN_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")

    from omniagentos.sessions import token as token_mod

    token_path = token_mod.token_path()
    assert not token_path.exists()

    from omniagentos.api import app as production_app

    with TestClient(production_app):
        assert not token_path.exists()
