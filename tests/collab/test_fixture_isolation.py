"""Regression coverage for the shared FastAPI fixture boundary."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.sessions.discover import discovery_enabled


@pytest.fixture
def inherited_override() -> Iterator[tuple[Any, Any]]:
    """Install state that the ASGI fixture must preserve exactly."""
    previous = dict(app.dependency_overrides)

    def dependency() -> None:
        return None

    def override() -> str:
        return "inherited"

    app.dependency_overrides[dependency] = override
    try:
        yield dependency, override
        assert app.dependency_overrides.get(dependency) is override
    finally:
        app.dependency_overrides = previous


def test_asgi_fixture_is_hermetic_and_restores_prior_overrides(
    inherited_override: tuple[Any, Any],
    asgi_client: httpx.AsyncClient,
) -> None:
    dependency, override = inherited_override
    assert discovery_enabled() is False
    assert app.dependency_overrides.get(dependency) is override
    assert asgi_client.base_url == "http://testserver"
