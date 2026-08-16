"""Tests for the API chokepoint middleware (observation-only).

The chokepoint middleware observes all requests and records structural
violations to a JSONL ledger without blocking any requests.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from omniagentos.api.middleware.chokepoint import (
    _BREAKER_RESET_PATH,
    _analyze_request_body,
    _check_array_lengths,
    _compute_structural_depth,
    _get_ledger_path,
    _read_breaker_state,
)


def _make_deep_object(depth: int) -> dict[str, object]:
    """Create a deeply nested object with the specified depth."""
    result: dict[str, object] = {f"level_{depth}": 1}
    for i in range(depth - 1, 0, -1):
        result = {f"level_{i}": result}
    return result


def _make_large_array(length: int) -> dict[str, object]:
    """Create an object with a large array."""
    return {"items": list(range(length))}


class TestStructuralDepthComputation:
    """Test structural depth analysis."""

    def test_flat_object_depth(self) -> None:
        """Flat objects should have depth 1."""
        obj = {"a": 1, "b": 2}
        depth, exceeded = _compute_structural_depth(obj, max_depth=20)
        assert depth == 1
        assert not exceeded

    def test_nested_object_depth(self) -> None:
        """Nested objects should compute correctly."""
        obj = {"a": {"b": {"c": 1}}}
        depth, exceeded = _compute_structural_depth(obj, max_depth=20)
        assert depth == 3
        assert not exceeded

    def test_deep_nesting_exceeds(self) -> None:
        """Deep nesting should exceed the cap."""
        obj = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        depth, exceeded = _compute_structural_depth(obj, max_depth=3)
        assert depth > 3
        assert exceeded

    def test_mixed_nested_structure(self) -> None:
        """Mixed arrays and objects should compute max depth."""
        obj = {"a": [{"b": [1, 2, 3]}]}
        depth, exceeded = _compute_structural_depth(obj, max_depth=20)
        assert depth == 4
        assert not exceeded

    def test_empty_containers(self) -> None:
        """Empty containers should not add depth."""
        obj = {"a": [], "b": {}}
        depth, exceeded = _compute_structural_depth(obj, max_depth=20)
        assert depth == 1
        assert not exceeded


class TestArrayLengthCheck:
    """Test array length analysis."""

    def test_short_array(self) -> None:
        """Short arrays should not exceed cap."""
        obj = {"items": [1, 2, 3]}
        max_len, exceeded = _check_array_lengths(obj, max_length=1000)
        assert max_len == 3
        assert not exceeded

    def test_long_array_exceeds(self) -> None:
        """Long arrays should exceed cap."""
        obj = _make_large_array(2000)
        max_len, exceeded = _check_array_lengths(obj, max_length=1000)
        assert max_len == 2000
        assert exceeded

    def test_nested_arrays(self) -> None:
        """Should find max array in nested structure."""
        obj = {"a": [1, 2], "b": {"c": list(range(1500))}}
        max_len, exceeded = _check_array_lengths(obj, max_length=1000)
        assert max_len == 1500
        assert exceeded

    def test_empty_array(self) -> None:
        """Empty arrays should not trigger exceeded."""
        obj = {"items": []}
        max_len, exceeded = _check_array_lengths(obj, max_length=1000)
        assert max_len == 0
        assert not exceeded


class TestAnalyzeRequestBody:
    """Test request body analysis."""

    def test_non_json_body(self) -> None:
        """Non-JSON bodies should not trigger analysis."""
        result = _analyze_request_body(b"plain text", "text/plain")
        assert result["body_depth"] is None
        assert result["max_array_length"] is None
        assert not result["body_depth_exceeded"]
        assert not result["array_length_exceeded"]

    def test_empty_body(self) -> None:
        """Empty bodies should not trigger analysis."""
        result = _analyze_request_body(b"", "application/json")
        assert result["body_depth"] is None
        assert result["max_array_length"] is None

    def test_invalid_json_body(self) -> None:
        """Invalid JSON should be handled gracefully."""
        result = _analyze_request_body(b"{invalid json}", "application/json")
        assert result["body_depth"] is None
        assert result["max_array_length"] is None

    def test_valid_json_body(self) -> None:
        """Valid JSON should be analyzed."""
        body = json.dumps({"a": {"b": [1, 2, 3]}})
        result = _analyze_request_body(body.encode(), "application/json")
        assert result["body_depth"] == 3
        assert result["max_array_length"] == 3
        assert not result["body_depth_exceeded"]
        assert not result["array_length_exceeded"]

    def test_exceeded_depth_flagged(self) -> None:
        """Exceeded depth should be flagged."""
        obj = _make_deep_object(25)
        body = json.dumps(obj)
        result = _analyze_request_body(body.encode(), "application/json")
        assert result["body_depth_exceeded"]

    def test_exceeded_array_length_flagged(self) -> None:
        """Exceeded array length should be flagged."""
        obj = _make_large_array(2000)
        body = json.dumps(obj)
        result = _analyze_request_body(body.encode(), "application/json")
        assert result["array_length_exceeded"]


class TestLedgerPath:
    """Test ledger path resolution."""

    def test_ledger_path_ends_with_jsonl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ledger path should be a .jsonl file."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        path = _get_ledger_path()
        assert path.name == "api-chokepoint-ledger.jsonl"

    def test_ledger_path_uses_var_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ledger path should use OMNIAGENTOS_VAR_DIR when set."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        path = _get_ledger_path()
        assert path.parent == tmp_path
        assert path.name == "api-chokepoint-ledger.jsonl"

    def test_ledger_path_not_in_package(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ledger path should never be inside the package directory."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        path = _get_ledger_path()
        # Verify it's not under the omniagentos package dir
        package_root = Path(__file__).resolve().parents[2] / "omniagentos"
        assert not str(path).startswith(str(package_root))


class TestMiddlewareIntegration:
    """Integration tests with the actual middleware."""

    def test_middleware_observes_simple_get(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Middleware should observe simple GET requests."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        response = asyncio.run(asgi_client.get("/api/system/map"))
        # Should get a response (might be 401 for auth, but not middleware-rejected)
        assert response.status_code in (200, 401)

    def test_middleware_writes_ledger(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Middleware should write observations to ledger."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        # Make a request (GET, no auth required)
        asyncio.run(asgi_client.get("/api/system/map"))

        # Check ledger was created and contains entries
        ledger_path = tmp_path / "api-chokepoint-ledger.jsonl"
        assert ledger_path.exists()

        # Parse ledger entries
        entries = []
        with ledger_path.open("r") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))

        # Should have at least one entry
        assert len(entries) > 0

        # Check entry structure
        entry = entries[0]
        assert "path" in entry
        assert "method" in entry
        assert "content_length" in entry
        assert "would_reject_under_proposed_policy" in entry

    def test_ledger_contains_method_and_path(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ledger entries should contain method and path."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        asyncio.run(asgi_client.get("/api/system/map"))

        ledger_path = tmp_path / "api-chokepoint-ledger.jsonl"
        with ledger_path.open("r") as f:
            line = f.readline()
            entry = json.loads(line)

        assert entry["method"] == "GET"
        assert entry["path"] == "/api/system/map"

    def test_middleware_never_rejects_when_breaker_not_tripped(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a tripped breaker, middleware does not invent rejection codes."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        # Fresh install: no state file → not tripped → no 503 from the breaker.
        response = asyncio.run(asgi_client.get("/api/system/map"))
        assert response.status_code != 429
        assert response.status_code != 503

    def test_middleware_json_ledger_format(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ledger should be valid JSONL (one JSON object per line)."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        # Make multiple requests
        for i in range(3):
            asyncio.run(asgi_client.get(f"/api/system/map?test={i}"))

        ledger_path = tmp_path / "api-chokepoint-ledger.jsonl"
        assert ledger_path.exists()

        # Each line should be valid JSON
        with ledger_path.open("r") as f:
            line_count = 0
            for line in f:
                if line.strip():
                    obj = json.loads(line)  # Should not raise
                    assert isinstance(obj, dict)
                    line_count += 1

        assert line_count >= 3

    def test_middleware_passes_through_status_codes(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Middleware should not modify response status codes."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        # 404 should still be 404
        response = asyncio.run(asgi_client.get("/api/nonexistent"))
        assert response.status_code == 404

        # 401 should still be 401 (for protected routes without token)
        response = asyncio.run(asgi_client.get("/api/system/map"))
        assert response.status_code in (200, 401)  # Either works or needs auth

    def test_ledger_rows_are_dated(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every ledger row carries the UTC instant it was written.

        ``dispatch`` builds the observation with ``"timestamp": None`` and the
        comment ``# Will be recorded on write``; the write path is the only
        place that can honour it. An append-only ledger whose whole purpose is
        offline policy analysis is unorderable and unwindowable without this,
        and nothing else in the record identifies when the request happened.
        """
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        asyncio.run(asgi_client.get("/api/system/map"))

        ledger_path = tmp_path / "api-chokepoint-ledger.jsonl"
        entry = json.loads(ledger_path.read_text().splitlines()[0])

        assert entry["timestamp"] is not None, (
            "ledger row is undated: dispatch defers the timestamp to the write "
            f"path and the write path never stamps one -- {entry}"
        )
        # Parseable as an aware UTC instant, not merely truthy.
        stamped = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
        assert stamped.tzinfo is not None, f"timestamp is not timezone-aware: {entry['timestamp']}"


class TestSpendBreakerEnforcement:
    """Spend circuit breaker: live reject of mutating methods when tripped."""

    def test_read_breaker_state_absent_is_not_tripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        state = _read_breaker_state()
        assert state == {"tripped": False}

    def test_read_breaker_state_corrupt_fails_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        (tmp_path / "spend-breaker-state.json").write_text("{not-json", encoding="utf-8")
        state = _read_breaker_state()
        assert state["tripped"] is True
        assert state["reason"] == "breaker_state_unreadable"

        (tmp_path / "spend-breaker-state.json").write_text(
            json.dumps({"oops": True}), encoding="utf-8"
        )
        state = _read_breaker_state()
        assert state["tripped"] is True
        assert state["reason"] == "breaker_state_unreadable"

    def test_read_breaker_state_legit_incoherent_sim_is_transparent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIM_MODE=1 but incoherent (no campaign): the app's route-level simgate
        governs, so the breaker stays TRANSPARENT (not tripped) rather than
        masking the coherent 403 with a 503 or crashing the request."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
        state = _read_breaker_state()
        assert state["tripped"] is False
        assert state["reason"] == "sim_env_incoherent"

    def test_read_breaker_state_production_sim_misconfig_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIM_MODE set to an invalid value (e.g. '0' — an operator 'disabling'
        sim by setting 0 instead of unsetting) is production-reachable and makes
        simgate refuse loudly. A spend breaker must FAIL CLOSED here, never open:
        returning not-tripped would silently disable the breaker on a config typo."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
        for bad in ("0", "false", "true", " 1", "1 "):
            monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", bad)
            state = _read_breaker_state()
            assert state["tripped"] is True, f"SIM_MODE={bad!r} must fail closed"
            assert state["reason"] == "sim_env_incoherent"

    def test_read_breaker_state_prod_unset_reads_the_real_state_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normal production (OMNIAGENTOS_SIM_MODE unset): the sim branch never
        fires, so _read_breaker_state reads the REAL state file verbatim — the
        SimGateError handling adds no behavior change to the production path."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)
        # A genuinely-tripped breaker on disk is read through as tripped.
        (tmp_path / "spend-breaker-state.json").write_text(
            json.dumps({"tripped": True, "reason": "spend_spike_intraday"}),
            encoding="utf-8",
        )
        state = _read_breaker_state()
        assert state == {"tripped": True, "reason": "spend_spike_intraday"}
        # And an explicitly not-tripped file reads through as not-tripped.
        (tmp_path / "spend-breaker-state.json").write_text(
            json.dumps({"tripped": False}), encoding="utf-8"
        )
        assert _read_breaker_state() == {"tripped": False}

    def test_post_denied_when_tripped_get_still_allowed(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        (tmp_path / "spend-breaker-state.json").write_text(
            json.dumps(
                {
                    "tripped": True,
                    "reason": "Intraday advertising spend cap exceeded",
                    "rule": "spend_spike_intraday",
                }
            ),
            encoding="utf-8",
        )

        post = asyncio.run(asgi_client.post("/api/system/map", json={"x": 1}))
        assert post.status_code == 503
        body = post.json()
        assert "circuit breaker" in body["detail"].casefold() or "paused" in body["detail"].casefold()

        get = asyncio.run(asgi_client.get("/api/system/map"))
        assert get.status_code != 503

        ledger_path = tmp_path / "api-chokepoint-ledger.jsonl"
        entries = [
            json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line
        ]
        post_entries = [e for e in entries if e.get("method") == "POST"]
        assert post_entries
        assert post_entries[0]["breaker_tripped"] is True
        assert post_entries[0]["rejected"] is True
        assert post_entries[0]["would_reject_under_proposed_policy"] is True

    def test_post_allowed_when_not_tripped(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        # Explicit not-tripped state file.
        (tmp_path / "spend-breaker-state.json").write_text(
            json.dumps({"tripped": False}), encoding="utf-8"
        )
        response = asyncio.run(asgi_client.post("/api/system/map", json={"x": 1}))
        # Downstream may 401/404/405 — breaker must not invent a 503.
        assert response.status_code != 503

    def test_post_denied_when_state_file_corrupt(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        (tmp_path / "spend-breaker-state.json").write_text("%%%", encoding="utf-8")
        response = asyncio.run(asgi_client.post("/api/nonexistent", json={}))
        assert response.status_code == 503

    def test_reset_path_falls_through_the_breaker_block_but_is_not_500d(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F2 round 1 regression, narrowed for round 2: the breaker must not
        wedge itself shut, but the exemption is ONLY "don't synthesize a 503
        here" -- it must NOT itself authenticate/clear anything (that would be
        the round-1 auth bypass). This asserts the middleware-level half only:
        a normal mutating path stays 503'd while tripped, and the reset path
        is never given a middleware-synthesized 503 (it falls through to
        routing instead, where auth is enforced -- see the auth tests below).
        """
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        state_path = tmp_path / "spend-breaker-state.json"
        state_path.write_text(
            json.dumps({"tripped": True, "reason": "test"}), encoding="utf-8"
        )

        # A normal mutating request is refused while tripped (sanity baseline).
        pre = asyncio.run(asgi_client.post("/api/system/map", json={}))
        assert pre.status_code == 503

        # The reset path is never 503'd by the breaker it exists to clear --
        # but note this alone does NOT mean it succeeded; it just means the
        # request reached routing (auth boundary tested separately below).
        reset = asyncio.run(asgi_client.post(_BREAKER_RESET_PATH, json={}))
        assert reset.status_code != 503

        # State must be UNCHANGED by an unauthenticated call -- the middleware
        # itself never writes the state file (only the authenticated route
        # does, and only after auth passes).
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["tripped"] is True

    def test_non_post_reset_path_is_not_exempted_while_tripped(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FF-1: the reset-path exemption from the breaker's 503 must be
        scoped to POST, because the real reset route is POST-only
        (``@router.post("/spend-breaker/reset")``). Before the fix,
        ``is_reset_path`` checked the URL path only, so ANY mutating method
        (PUT/PATCH/DELETE) to the reset path fell through the 503 block --
        widening the exemption surface beyond the real endpoint. A tripped
        PUT/PATCH/DELETE to the reset path must still get the breaker's 503.
        """
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        state_path = tmp_path / "spend-breaker-state.json"
        state_path.write_text(
            json.dumps({"tripped": True, "reason": "test"}), encoding="utf-8"
        )

        for method in ("PUT", "PATCH", "DELETE"):
            response = asyncio.run(
                asgi_client.request(method, _BREAKER_RESET_PATH, json={})
            )
            assert response.status_code == 503, (
                f"{method} {_BREAKER_RESET_PATH} while tripped must be "
                f"503'd by the breaker (the reset route is POST-only), got "
                f"{response.status_code}: {response.text}"
            )

        # State must be unchanged -- none of the non-POST attempts reached
        # (or cleared) anything.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["tripped"] is True

        # Positive control: a tripped POST to the reset path is still
        # exempted from the middleware's 503 (it falls through to routing,
        # where auth is enforced separately) -- the fix must not break the
        # real reset path.
        post = asyncio.run(asgi_client.post(_BREAKER_RESET_PATH, json={}))
        assert post.status_code != 503

    def test_tokenless_reset_is_rejected_and_breaker_stays_tripped(
        self, asgi_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1 (round 2) regression: the round-1 reset exemption let a
        TOKENLESS POST to the reset path clear the breaker, because the
        middleware handled (and auth-bypassed) it before routing ever ran.
        This is the exact scenario the runaway agent this breaker exists to
        contain would exploit. Asserts: no X-Session-Token header -> reset is
        REJECTED (401/403) and the on-disk breaker state is untouched.
        """
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        state_path = tmp_path / "spend-breaker-state.json"
        state_path.write_text(
            json.dumps({"tripped": True, "reason": "test"}), encoding="utf-8"
        )

        response = asyncio.run(asgi_client.post(_BREAKER_RESET_PATH, json={}))
        assert response.status_code in (401, 403), (
            f"expected the auth gate to reject a tokenless reset, got "
            f"{response.status_code}: {response.text}"
        )

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["tripped"] is True, "an unauthenticated request must NEVER clear the breaker"

        # And the breaker still refuses normal mutating traffic -- the reject
        # did not have a side effect of un-tripping anything.
        post = asyncio.run(asgi_client.post("/api/system/map", json={}))
        assert post.status_code == 503

    def test_authenticated_reset_clears_the_breaker_and_unblocks_mutations(
        self,
        asgi_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        auth_headers: dict[str, str],
    ) -> None:
        """The intended path: a caller holding the real session token can
        un-trip the breaker, and mutating traffic is unblocked afterward.
        """
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        state_path = tmp_path / "spend-breaker-state.json"
        state_path.write_text(
            json.dumps({"tripped": True, "reason": "test"}), encoding="utf-8"
        )

        reset = asyncio.run(
            asgi_client.post(_BREAKER_RESET_PATH, json={}, headers=auth_headers)
        )
        assert reset.status_code == 200, reset.text
        assert reset.json() == {"tripped": False, "cleared": True}

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["tripped"] is False

        # Un-tripped: a normal mutating request now passes the breaker check
        # (it may still 401/404/405 downstream -- that is not this middleware).
        post_reset = asyncio.run(asgi_client.post("/api/system/map", json={}))
        assert post_reset.status_code != 503

    def test_authenticated_reset_is_idempotent_when_already_not_tripped(
        self,
        asgi_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        auth_headers: dict[str, str],
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
        # No state file at all yet -- reset must still succeed cleanly.
        response = asyncio.run(
            asgi_client.post(_BREAKER_RESET_PATH, json={}, headers=auth_headers)
        )
        assert response.status_code == 200
        assert response.json()["tripped"] is False
