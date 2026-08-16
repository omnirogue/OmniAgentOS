"""Tests for GitHub source adapter + session correlation + ingress wiring."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes import comms as comms_routes
from omniagentos.comms.correlation import attribute_github_event
from omniagentos.comms.github_mode import GITHUB_COMMS_ENV, verify_github_signature
from omniagentos.comms.normalize import normalize_github, process_github_event
from omniagentos.steward.config import CommsConfig, InboundSourceCfg, StewardConfig

_FIXTURES = Path(__file__).parent / "fixtures" / "github"
_FROZEN_KEYS = {
    "source",
    "external_id",
    "thread_id",
    "sender",
    "recipients",
    "sent_at",
    "subject",
    "body_text",
    "attachments",
    "raw",
}
GITHUB_SECRET_ENV = "COMMS_WEBHOOK_SECRET_GITHUB"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_pr_comment_normalizes_and_attributes() -> None:
    payload = _load("pr_comment.json")
    message = normalize_github(payload, event_type="issue_comment")
    assert message is not None
    assert set(message) == _FROZEN_KEYS
    assert message["source"] == "github"
    assert "ses_abc123def456" in message["body_text"]
    corr = attribute_github_event(
        {
            "body": message["body_text"],
            "head_ref": message["raw"].get("head_ref"),
            "pull_request": payload.get("pull_request"),
            "comment": payload.get("comment"),
        }
    )
    assert corr.attribution == "session"
    assert corr.session_id == "ses_abc123def456"


def test_ci_failure_normalizes_and_attributes() -> None:
    payload = _load("ci_failure.json")
    message = normalize_github(payload, event_type="check_run")
    assert message is not None
    assert message["source"] == "github"
    assert "failure" in message["body_text"].lower() or "pytest" in message["body_text"]
    corr = attribute_github_event(
        {
            "body": message["body_text"],
            "head_ref": message["raw"].get("head_ref"),
            "check_run": payload.get("check_run"),
        }
    )
    assert corr.attribution == "session"
    assert corr.session_id == "ses_cafebabef00d"


def test_unmatchable_is_unattributed() -> None:
    payload = {
        "comment": {"id": 1, "body": "no markers here", "user": {"login": "x"}},
        "issue": {"number": 1},
        "repository": {"full_name": "acme/app"},
    }
    message = normalize_github(payload, event_type="issue_comment")
    assert message is not None
    corr = attribute_github_event({"body": message["body_text"]})
    assert corr.attribution == "unattributed"
    assert corr.session_id is None


def test_unknown_event_ignored() -> None:
    payload = _load("unknown_event.json")
    assert normalize_github(payload, event_type="star") is None
    assert normalize_github(payload) is None


def test_shadow_delivers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_COMMS_ENV, "shadow")
    delivered: list[object] = []

    def deliver(message: object, correlation: object) -> None:
        delivered.append((message, correlation))

    payload = _load("pr_comment.json")
    result = process_github_event(
        payload,
        event_type="issue_comment",
        env={GITHUB_COMMS_ENV: "shadow"},
        deliver=deliver,
    )
    assert result is not None
    assert result["mode"] == "shadow"
    assert result["delivered"] is False
    assert delivered == []
    assert result["correlation"]["session_id"] == "ses_abc123def456"


def test_off_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_COMMS_ENV, "off")
    result = process_github_event(
        _load("pr_comment.json"),
        event_type="issue_comment",
        env={GITHUB_COMMS_ENV: "off"},
    )
    assert result is None


def test_enforce_delivers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_COMMS_ENV, "enforce")
    delivered: list[object] = []

    def deliver(message: object, correlation: object) -> None:
        delivered.append((message, correlation))

    result = process_github_event(
        _load("pr_comment.json"),
        event_type="issue_comment",
        env={GITHUB_COMMS_ENV: "enforce"},
        deliver=deliver,
    )
    assert result is not None
    assert result["delivered"] is True
    assert len(delivered) == 1


def test_verify_github_signature_round_trip() -> None:
    body = b'{"action":"created"}'
    secret = "whsec_test"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, secret, f"sha256={digest}") is True
    assert verify_github_signature(body, secret, "sha256=deadbeef") is False
    assert verify_github_signature(body, secret, None) is False
    assert verify_github_signature(body, "", f"sha256={digest}") is False


def test_github_ingress_signature_auth_correlation_delivery(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAJOR: GitHub adapter on real /api/comms/inbound (signature, correlation, delivery)."""
    secret = "github-ingress-secret"
    monkeypatch.setenv(GITHUB_SECRET_ENV, secret)
    monkeypatch.setenv(GITHUB_COMMS_ENV, "enforce")

    cfg = StewardConfig()
    cfg.comms = CommsConfig(
        inbound_max_bytes=65536,
        rate_limit_per_minute=60,
        sources={"github": InboundSourceCfg(secret_env=GITHUB_SECRET_ENV)},
    )
    app.dependency_overrides[comms_routes.get_steward_config] = lambda: cfg
    comms_routes.reset_rate_limits()

    payload = _load("pr_comment.json")
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    async def _post(sig: str | None) -> httpx.Response:
        headers: dict[str, str] = {"X-GitHub-Event": "issue_comment"}
        if sig is not None:
            headers["X-Hub-Signature-256"] = sig
        return await asgi_client.post(
            "/api/comms/inbound",
            params={"source": "github"},
            content=body,
            headers=headers,
        )

    denied = asyncio.run(_post("sha256=" + ("00" * 32)))
    assert denied.status_code == 401

    ok = asyncio.run(_post(f"sha256={digest}"))
    assert ok.status_code in {200, 202}
    data = ok.json()
    assert data.get("mode") == "enforce"
    assert data.get("delivered") is True or data.get("created") is True
    assert data.get("correlation", {}).get("session_id") == "ses_abc123def456"
