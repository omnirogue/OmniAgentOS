"""Globex Studio local client (TN.9) — OmniAgentOS only.

Talks to a local studio process on 127.0.0.1:4700. Does not inject credentials;
generation is still capability-gated via the broker when used through toolplane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:4700"


@dataclass(frozen=True, slots=True)
class StudioResult:
    ok: bool
    status_code: int
    body: dict[str, Any] | str | None
    error: str | None = None


def generate_image(
    payload: dict[str, Any],
    *,
    base_url: str = DEFAULT_BASE,
    timeout_s: float = 120.0,
) -> StudioResult:
    """POST /v1/generate/image — returns structured result; never raises on HTTP errors."""
    return _post(f"{base_url.rstrip('/')}/v1/generate/image", payload, timeout_s)


def generate_video(
    payload: dict[str, Any],
    *,
    base_url: str = DEFAULT_BASE,
    timeout_s: float = 300.0,
) -> StudioResult:
    """POST /v1/generate/video."""
    return _post(f"{base_url.rstrip('/')}/v1/generate/video", payload, timeout_s)


def health(*, base_url: str = DEFAULT_BASE, timeout_s: float = 3.0) -> StudioResult:
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.get(f"{base_url.rstrip('/')}/health")
            body: dict[str, Any] | str | None
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            return StudioResult(ok=resp.is_success, status_code=resp.status_code, body=body)
    except httpx.HTTPError as exc:
        return StudioResult(ok=False, status_code=0, body=None, error=str(exc))


def _post(url: str, payload: dict[str, Any], timeout_s: float) -> StudioResult:
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=payload)
            try:
                body: dict[str, Any] | str | None = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            return StudioResult(
                ok=resp.is_success,
                status_code=resp.status_code,
                body=body,
                error=None if resp.is_success else f"http_{resp.status_code}",
            )
    except httpx.HTTPError as exc:
        return StudioResult(ok=False, status_code=0, body=None, error=str(exc))
