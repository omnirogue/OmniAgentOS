"""Tests for the Claude accounts management API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.db.store import SqliteStore
from tests.support.db_template import migrated_db


def _init_db(tmp_path: Path) -> str:
    """Initialize a test database with all migrations applied."""
    db = str(tmp_path / "test.db")
    return migrated_db(SqliteStore, db)


def test_get_accounts_returns_empty_list(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET /accounts returns accounts list (may be empty if no auto-detected dirs)."""
    db_path = _init_db(tmp_path)
    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    # No auto-detection (empty list)
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(asgi_client.get("/api/accounts"))
    assert response.status_code == 200
    data = response.json()
    assert "accounts" in data
    assert isinstance(data["accounts"], list)


def test_post_accounts_creates_config_dir_account(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /accounts with a valid config_dir creates an account."""
    db_path = _init_db(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "test-account",
                "auth_type": "config_dir",
                "config_dir": str(config_dir),
                "enabled": True,
            },
        )
    )

    assert response.status_code == 201
    account = response.json()
    assert account["label"] == "test-account"
    assert account["auth_type"] == "config_dir"
    assert account["config_dir"] == str(config_dir)
    assert account["enabled"] is True
    assert "has_secret" in account
    assert account["has_secret"] is False  # config_dir accounts have no secret
    assert "secret_ref" not in account  # secrets never in response


def test_post_accounts_with_nonexistent_config_dir_fails(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /accounts with nonexistent config_dir returns 400."""
    db_path = _init_db(tmp_path)
    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "bad-account",
                "auth_type": "config_dir",
                "config_dir": "/nonexistent/path",
            },
        )
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation"
    assert "does not exist" in error["message"]


def test_post_accounts_duplicate_config_dir_fails(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /accounts with a duplicate config_dir returns 400."""
    db_path = _init_db(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    # First account succeeds
    response1 = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "first",
                "auth_type": "config_dir",
                "config_dir": str(config_dir),
            },
        )
    )
    assert response1.status_code == 201

    # Second account with same config_dir fails
    response2 = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "second",
                "auth_type": "config_dir",
                "config_dir": str(config_dir),
            },
        )
    )
    assert response2.status_code == 400
    error = response2.json()["error"]
    assert error["code"] == "validation"
    assert "already registered" in error["message"]


def test_post_accounts_with_oauth_token(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /accounts with oauth_token type stores secret in var/secrets."""
    db_path = _init_db(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])
    # Mock the secrets dir to a temp location
    monkeypatch.setattr(
        "omniagentos.accounts.service._secrets_dir",
        lambda: secrets_dir,
    )

    response = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "oauth-account",
                "auth_type": "oauth_token",
                "secret": "sk-fake-token-12345",
                "enabled": True,
            },
        )
    )

    assert response.status_code == 201
    account = response.json()
    assert account["auth_type"] == "oauth_token"
    assert account["has_secret"] is True
    assert "secret_ref" not in account  # raw secret never in response
    assert account["config_dir"] is None


@pytest.mark.real_auth
def test_post_accounts_requires_auth(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /accounts without session token returns 401."""
    db_path = _init_db(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            json={
                "label": "unauthorized",
                "config_dir": str(config_dir),
            },
        )
    )

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"


def test_patch_account_toggle_enabled(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PATCH /accounts/{id} can toggle the enabled flag."""
    db_path = _init_db(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    # Create account
    create_response = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "test",
                "config_dir": str(config_dir),
                "enabled": True,
            },
        )
    )
    account_id = create_response.json()["id"]

    # Disable it
    response = asyncio.run(
        asgi_client.patch(
            f"/api/accounts/{account_id}",
            headers=auth_headers,
            json={"enabled": False},
        )
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["enabled"] is False

    # Enable it again
    response2 = asyncio.run(
        asgi_client.patch(
            f"/api/accounts/{account_id}",
            headers=auth_headers,
            json={"enabled": True},
        )
    )

    assert response2.status_code == 200
    updated2 = response2.json()
    assert updated2["enabled"] is True


def test_patch_account_set_default(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PATCH /accounts/{id} with is_default=true makes it the default."""
    db_path = _init_db(tmp_path)
    config_dir1 = tmp_path / "config1"
    config_dir1.mkdir()
    config_dir2 = tmp_path / "config2"
    config_dir2.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    # Create two accounts
    asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "account1",
                "config_dir": str(config_dir1),
            },
        )
    )

    resp2 = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "account2",
                "config_dir": str(config_dir2),
            },
        )
    )
    account2_id = resp2.json()["id"]

    # Make account2 the default
    response = asyncio.run(
        asgi_client.patch(
            f"/api/accounts/{account2_id}",
            headers=auth_headers,
            json={"is_default": True},
        )
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["is_default"] is True
    assert updated["enabled"] is True  # default is implicitly enabled

    # Check account1 is no longer default
    response1 = asyncio.run(
        asgi_client.get(
            "/api/accounts",
        )
    )
    accounts = response1.json()["accounts"]
    defaults = [a for a in accounts if a["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == account2_id


def test_patch_account_not_found(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PATCH /accounts/{id} with unknown id returns 404."""
    db_path = _init_db(tmp_path)
    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.patch(
            "/api/accounts/nonexistent-id",
            headers=auth_headers,
            json={"enabled": False},
        )
    )

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"


@pytest.mark.real_auth
def test_patch_account_requires_auth(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PATCH /accounts/{id} without session token returns 401."""
    db_path = _init_db(tmp_path)
    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.patch(
            "/api/accounts/some-id",
            json={"enabled": False},
        )
    )

    assert response.status_code == 401


def test_delete_account(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DELETE /accounts/{id} removes an account."""
    db_path = _init_db(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    # Create account
    create_response = asyncio.run(
        asgi_client.post(
            "/api/accounts",
            headers=auth_headers,
            json={
                "label": "to-delete",
                "config_dir": str(config_dir),
            },
        )
    )
    account_id = create_response.json()["id"]

    # Delete it
    response = asyncio.run(
        asgi_client.delete(
            f"/api/accounts/{account_id}",
            headers=auth_headers,
        )
    )

    assert response.status_code == 200
    result = response.json()
    assert result["removed"] is True

    # Verify it's gone
    list_response = asyncio.run(asgi_client.get("/api/accounts"))
    accounts = list_response.json()["accounts"]
    assert not any(a["id"] == account_id for a in accounts)


def test_delete_account_not_found(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DELETE /accounts/{id} with unknown id returns 404."""
    db_path = _init_db(tmp_path)
    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.delete(
            "/api/accounts/nonexistent-id",
            headers=auth_headers,
        )
    )

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"


@pytest.mark.real_auth
def test_delete_account_requires_auth(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DELETE /accounts/{id} without session token returns 401."""
    db_path = _init_db(tmp_path)
    monkeypatch.setattr(
        "omniagentos.accounts.service.default_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    response = asyncio.run(
        asgi_client.delete(
            "/api/accounts/some-id",
        )
    )

    assert response.status_code == 401
