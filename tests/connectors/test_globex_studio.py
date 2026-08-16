"""Globex Studio client — handles offline studio without crash."""

from __future__ import annotations

from omniagentos.connectors.globex_studio import generate_image, health


def test_health_offline_is_structured() -> None:
    # Studio usually not running in CI — must not raise.
    r = health(timeout_s=0.5)
    assert r.ok is False or r.status_code in {200, 404}
    assert r.error is not None or r.body is not None or r.status_code == 200


def test_generate_image_offline() -> None:
    r = generate_image({"prompt": "test"}, timeout_s=0.5)
    assert r.ok is False or isinstance(r.body, (dict, str))
