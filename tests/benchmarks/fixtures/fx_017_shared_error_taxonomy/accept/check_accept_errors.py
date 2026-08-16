"""
FROZEN acceptance check for fx_017_shared_error_taxonomy.
This file is copied in after the agent completes the task so the agent cannot modify or weaken it.
It verifies that all three surfaces derive their error taxonomy and behaviours dynamically
from the single shared errors.REGISTRY.
"""

from __future__ import annotations

import api
import cli
import errors
import worker


def test_registry_definitions() -> None:
    # 1. Assert the five registry entries exactly
    expected = {
        "E_NOT_FOUND": (404, False, 4),
        "E_CONFLICT": (409, False, 5),
        "E_RATE_LIMITED": (429, True, 6),
        "E_TIMEOUT": (504, True, 7),
        "E_INTERNAL": (500, True, 1),
    }

    assert len(errors.REGISTRY) == 5
    for code, (status, retryable, exit_code) in expected.items():
        spec = errors.spec_for(code)
        assert spec.code == code
        assert spec.http_status == status
        assert spec.retryable is retryable
        assert spec.exit_code == exit_code


def test_domain_exceptions_agreement() -> None:
    # 2. For every domain exception, assert all three surfaces agree with spec_for(exc.code)
    # Also assert that every code in REGISTRY is reachable from at least one exception class.
    exceptions_map = {
        "E_NOT_FOUND": errors.NotFound("missing"),
        "E_CONFLICT": errors.Conflict("conflict"),
        "E_RATE_LIMITED": errors.RateLimited("rate limited"),
        "E_TIMEOUT": errors.Timeout("timeout"),
        "E_INTERNAL": errors.Internal("internal"),
    }

    assert set(exceptions_map.keys()) == set(errors.REGISTRY.keys())

    for code, exc in exceptions_map.items():
        spec = errors.spec_for(code)

        # Verify api.to_response
        resp = api.to_response(exc)
        assert resp == {
            "code": code,
            "status": spec.http_status,
            "retryable": spec.retryable,
        }

        # Verify worker surface
        assert worker.should_retry(exc) is spec.retryable
        assert worker.dead_letter_reason(exc) == f"{code}: {exc}"

        # Verify cli surface
        assert cli.exit_code_for(exc) == spec.exit_code
        assert cli.render(exc) == f"error [{code}]: {exc}"


def test_non_domain_exception_defaults() -> None:
    # 3. Assert a plain RuntimeError maps to E_INTERNAL on all three surfaces
    exc = RuntimeError("unexpected crash")
    spec = errors.spec_for("E_INTERNAL")

    resp = api.to_response(exc)
    assert resp == {
        "code": "E_INTERNAL",
        "status": spec.http_status,
        "retryable": spec.retryable,
    }
    assert worker.should_retry(exc) is spec.retryable
    assert worker.dead_letter_reason(exc) == f"E_INTERNAL: {exc}"
    assert cli.exit_code_for(exc) == spec.exit_code
    assert cli.render(exc) == f"error [E_INTERNAL]: {exc}"


def test_decisive_derivation() -> None:
    # 4. Define a NEW AppError subclass with a new code, insert a matching ErrorSpec
    # and assert all three surfaces immediately honour it with no code change.

    original_registry = dict(errors.REGISTRY)

    class MockCustomError(errors.AppError):
        code = "E_CUSTOM_INTEGRATION"

    custom_spec = errors.ErrorSpec(
        code="E_CUSTOM_INTEGRATION", http_status=418, retryable=True, exit_code=99
    )

    errors.REGISTRY["E_CUSTOM_INTEGRATION"] = custom_spec

    try:
        exc = MockCustomError("teapot")

        # All surfaces must immediately work with no hardcoding
        resp = api.to_response(exc)
        assert resp == {
            "code": "E_CUSTOM_INTEGRATION",
            "status": 418,
            "retryable": True,
        }
        assert worker.should_retry(exc) is True
        assert worker.dead_letter_reason(exc) == "E_CUSTOM_INTEGRATION: teapot"
        assert cli.exit_code_for(exc) == 99
        assert cli.render(exc) == "error [E_CUSTOM_INTEGRATION]: teapot"

    finally:
        # Restore registry
        errors.REGISTRY.clear()
        errors.REGISTRY.update(original_registry)


def test_all_codes_sorted() -> None:
    # 5. Assert all_codes() is sorted and covers the registry
    codes = errors.all_codes()
    assert len(codes) == len(errors.REGISTRY)
    assert codes == tuple(sorted(errors.REGISTRY.keys()))
