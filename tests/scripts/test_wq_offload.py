"""wq_offload.py — fail-closed unit construction, label passthrough, auth header.

HTTP is stubbed at ``urllib.request.urlopen`` (the transport HttpQueueClient
actually uses), so the auth test proves the real header reaches the real wire
call — not a mock of our own mock.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from scripts.ops import wq_offload

SHA = "ccb059a921e2694f649fb8ac0b41dfca363d300b"


# --------------------------------------------------------- unit construction --
def test_build_unit_fail_closed_fields() -> None:
    unit = wq_offload.build_unit(base_sha=SHA, tests="tests/taskcontract", submitted_by="owner")
    # Every contract-required field is present and correctly shaped.
    for required in (
        "idempotency_key",
        "repo_url",
        "repo_slug",
        "base_sha",
        "branch",
        "owned_paths",
        "agent_profile",
        "acceptance_cmd",
        "risk_class",
    ):
        assert unit[required], f"missing required field {required}"
    # Fail-closed: the acceptance command IS the pytest command — no wrapper.
    assert unit["acceptance_cmd"] == "uv run --frozen pytest -q tests/taskcontract"
    assert unit["agent_profile"] == "script"
    assert unit["acceptance_gate"] is None
    assert unit["risk_class"] == "mechanical"
    assert unit["base_sha"] == SHA
    assert unit["owned_paths"] == ["var/wq-offload/**"]  # minimal, never written to
    assert unit["submitted_by"] == "owner"
    assert unit["labels"] == ["pytest"]
    assert unit["timeout_s"] == 900
    assert unit["max_attempts"] == 2
    assert unit["branch"].startswith("wq/offload-")


def test_build_unit_rejects_non_sha() -> None:
    with pytest.raises(ValueError, match="40-hex"):
        wq_offload.build_unit(base_sha="main", tests="tests/taskcontract")


def test_build_unit_rejects_empty_tests() -> None:
    with pytest.raises(ValueError, match="tests"):
        wq_offload.build_unit(base_sha=SHA, tests="   ")


def test_build_unit_label_and_owned_path_passthrough() -> None:
    unit = wq_offload.build_unit(
        base_sha=SHA,
        tests="tests/x",
        labels=["darwin", "pytest"],
        owned_paths=["var/custom/**"],
    )
    assert unit["labels"] == ["darwin", "pytest"]
    assert unit["owned_paths"] == ["var/custom/**"]


def test_idempotency_key_is_deterministic_until_fresh() -> None:
    a = wq_offload.build_unit(base_sha=SHA, tests="tests/x", submitted_by="")
    b = wq_offload.build_unit(base_sha=SHA, tests="tests/x", submitted_by="")
    c = wq_offload.build_unit(base_sha=SHA, tests="tests/y", submitted_by="")
    assert a["idempotency_key"] == b["idempotency_key"]  # same input dedupes
    assert a["idempotency_key"] != c["idempotency_key"]  # different tests = new key
    fresh = wq_offload.build_unit(base_sha=SHA, tests="tests/x", submitted_by="", fresh=True)
    assert fresh["idempotency_key"] != a["idempotency_key"]


def test_submitted_by_prefers_wq_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_USER", "owner")
    monkeypatch.setenv("USER", "someone-else")
    assert wq_offload.default_submitter() == "owner"
    monkeypatch.delenv("WQ_USER")
    assert wq_offload.default_submitter() == "someone-else"


# ------------------------------------------------------------------- token ----
def test_load_token_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_TOKEN", "tok-from-env")
    assert wq_offload.load_token() == "tok-from-env"


def test_load_token_from_connections_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WQ_TOKEN", raising=False)
    env_file = tmp_path / "connections.env"
    env_file.write_text('OTHER=x\nexport WQ_TOKEN="tok-from-file"\n', encoding="utf-8")
    monkeypatch.setattr(wq_offload, "CONNECTIONS_ENV", env_file)
    assert wq_offload.load_token() == "tok-from-file"


def test_load_token_missing_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WQ_TOKEN", raising=False)
    monkeypatch.setattr(wq_offload, "CONNECTIONS_ENV", tmp_path / "absent.env")
    assert wq_offload.load_token() == ""


# ------------------------------------------------------------ auth + enqueue --
class _FakeResponse:
    status = 200

    def read(self) -> bytes:
        return json.dumps({"id": "wq_test123", "deduped": False}).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_enqueue_sends_bearer_token_and_unit_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _FakeResponse:
        captured.append(request)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("WQ_TOKEN", raising=False)  # token must come from the argument
    unit = wq_offload.build_unit(base_sha=SHA, tests="tests/taskcontract", submitted_by="owner")
    unit_id, deduped = wq_offload.enqueue_unit(unit, "http://127.0.0.1:8487", "secret-tok")

    assert unit_id == "wq_test123"
    assert deduped is False
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "http://127.0.0.1:8487/v1/units"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret-tok"
    body = json.loads(request.data or b"{}")
    assert body["base_sha"] == SHA
    assert body["acceptance_cmd"] == "uv run --frozen pytest -q tests/taskcontract"
    assert body["submitted_by"] == "owner"


# -------------------------------------------------------------- ref resolve ----
def test_resolve_ref_accepts_full_sha_without_git() -> None:
    assert wq_offload.resolve_ref(SHA) == SHA


def test_resolve_ref_rejects_garbage(tmp_path: Path) -> None:
    # An empty git repo: nothing resolves, so the helper must refuse loudly.
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(SystemExit, match="cannot resolve"):
        wq_offload.resolve_ref("no-such-branch", repo_root=tmp_path)
