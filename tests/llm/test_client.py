"""Unit tests for the LiteLLM short-call client and budget guard."""

from __future__ import annotations

import json
import os
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from omniagentos.llm.budget import (
    BudgetGuard,
    LLMBudgetExceededError,
    LLMBudgetUnknownError,
    LLMClientError,
    LLMInvalidResponseError,
    LLMTransportError,
    _ledger_path,
)
from omniagentos.llm.client import ShortCallClient, _clean_and_parse_json


@pytest.fixture(autouse=True)
def isolate_var(monkeypatch, tmp_path):
    """Automatically isolate the OMNIAGENTOS_VAR directories to a tmp_path per test."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))


def test_clean_and_parse_json_simple():
    """Verify clean_and_parse_json works with plain JSON."""
    raw = '{"key": "value"}'
    assert _clean_and_parse_json(raw) == {"key": "value"}


def test_clean_and_parse_json_fenced_markdown():
    """Verify clean_and_parse_json strips fences and languages."""
    raw = '```json\n{\n  "key": "value"\n}\n```'
    assert _clean_and_parse_json(raw) == {"key": "value"}

    raw_no_lang = '```\n{"key": "value"}\n```'
    assert _clean_and_parse_json(raw_no_lang) == {"key": "value"}


def test_budget_guard_default_fallback(tmp_path):
    """Verify BudgetGuard resolves default values when yaml is missing."""
    non_existent = tmp_path / "configs" / "missing.yaml"
    guard = BudgetGuard(config_path=str(non_existent))

    assert guard.config["daily_usd_cap"] == 10.0
    assert guard.config["default_model"] == "gemini-3.6-flash"
    assert guard.config["proxy_base_url"] == "http://localhost:4000/v1"
    # Fallback rates should be available
    assert guard.config["rates"]["gemini-3.6-flash"]["input"] == 0.075


def test_budget_guard_record_spend():
    """Verify record_spend appends to ledger with correct cost estimation."""
    guard = BudgetGuard()
    # gemini-3.6-flash rate is $0.075 input, $0.30 output per million tokens
    cost = guard.record_spend("gemini-3.6-flash", prompt_tokens=100000, completion_tokens=200000)

    # Cost = (100,000 * 0.075 / 1M) + (200,000 * 0.30 / 1M)
    # Cost = 0.0075 + 0.06 = 0.0675
    assert abs(cost - 0.0675) < 1e-9

    ledger = _ledger_path()
    assert os.path.exists(ledger)

    with open(ledger, encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["model"] == "gemini-3.6-flash"
        assert entry["prompt_tokens"] == 100000
        assert entry["completion_tokens"] == 200000
        assert abs(entry["estimated_usd_cost"] - 0.0675) < 1e-9
        assert entry["purpose"] == "default"
        assert "timestamp" in entry


def test_budget_guard_unknown_model_fallback():
    """Verify BudgetGuard uses fallback rates for unknown models."""
    guard = BudgetGuard()
    # Fallback rates: input = 0.15, output = 0.60 per million tokens
    cost = guard.record_spend(
        "unreleased-giga-model", prompt_tokens=100000, completion_tokens=200000
    )

    # Cost = (100k * 0.15 / 1M) + (200k * 0.60 / 1M) = 0.015 + 0.12 = 0.135
    assert abs(cost - 0.135) < 1e-9


def test_budget_guard_malformed_lines_tolerated():
    """Verify malformed JSON/empty lines in the ledger do not crash spend aggregation."""
    ledger = _ledger_path()
    os.makedirs(os.path.dirname(ledger), exist_ok=True)

    with open(ledger, "w", encoding="utf-8") as f:
        f.write("\n")
        f.write("{malformed-json\n")
        # Today's valid entry
        import datetime

        today_str = datetime.datetime.now(datetime.UTC).isoformat()
        f.write(
            json.dumps(
                {"timestamp": today_str, "model": "gemini-3.6-flash", "estimated_usd_cost": 0.05}
            )
            + "\n"
        )

    guard = BudgetGuard()
    assert abs(guard.get_today_spend() - 0.05) < 1e-9


def test_budget_guard_refusal_at_or_over_cap():
    """Verify budget guard raises LLMBudgetExceededError when cap is reached."""
    config_override = {
        "daily_usd_cap": 0.01,
        "rates": {"gemini-3.6-flash": {"input": 1.0, "output": 1.0}},
    }
    guard = BudgetGuard(config_dict=config_override)

    # No spend yet: check should pass
    guard.check_budget()

    # Record spend over cap (100,000 * 1.0 / 1M) = 0.1 USD, which is > 0.01 USD daily cap
    guard.record_spend("gemini-3.6-flash", prompt_tokens=100000, completion_tokens=0)

    # Should now fail
    with pytest.raises(LLMBudgetExceededError) as exc_info:
        guard.check_budget()
    assert "spend budget exceeded" in str(exc_info.value)


@patch("urllib.request.urlopen")
def test_client_happy_path(mock_urlopen):
    """Verify happy path complete request."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "Short response text."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
    ).encode("utf-8")
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    client = ShortCallClient()
    res = client.complete([{"role": "user", "content": "hi"}], purpose="test-happy")

    assert res == "Short response text."
    mock_urlopen.assert_called_once()


@patch("urllib.request.urlopen")
def test_client_json_success(mock_urlopen):
    """Verify happy path json-mode request with schema key validation."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": '{"success": true, "code": 200}'}}
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 25},
        }
    ).encode("utf-8")
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    client = ShortCallClient()
    parsed = client.complete_json(
        messages=[{"role": "user", "content": "give json"}], required_keys=["success", "code"]
    )

    assert parsed == {"success": True, "code": 200}


@patch("urllib.request.urlopen")
def test_client_json_validation_failure_and_retry(mock_urlopen):
    """Verify JSON complete retries on missing required keys and raises LLMInvalidResponseError."""
    mock_resp_invalid = MagicMock()
    mock_resp_invalid.read.return_value = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": '{"partial": true}'}}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 25},
        }
    ).encode("utf-8")
    mock_resp_invalid.status = 200
    mock_resp_invalid.__enter__.return_value = mock_resp_invalid

    # Mock side_effect to return invalid both times (so we exhaust 1 retry)
    mock_urlopen.side_effect = [mock_resp_invalid, mock_resp_invalid]

    client = ShortCallClient()
    with pytest.raises(LLMInvalidResponseError) as exc_info:
        client.complete_json(
            messages=[{"role": "user", "content": "give json"}], required_keys=["full_key"]
        )

    assert "missing required keys" in str(exc_info.value)
    # 1 original try + 1 retry = 2 urlopen calls total
    assert mock_urlopen.call_count == 2


@patch("urllib.request.urlopen")
def test_client_json_success_after_one_invalid_try(mock_urlopen):
    """Verify JSON complete retries on invalid json once and then succeeds on second try."""
    mock_resp_invalid = MagicMock()
    mock_resp_invalid.read.return_value = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": '{"partial": true}'}}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 25},
        }
    ).encode("utf-8")
    mock_resp_invalid.status = 200
    mock_resp_invalid.__enter__.return_value = mock_resp_invalid

    mock_resp_valid = MagicMock()
    mock_resp_valid.read.return_value = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"partial": true, "full_key": "some-value"}',
                    }
                }
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 25},
        }
    ).encode("utf-8")
    mock_resp_valid.status = 200
    mock_resp_valid.__enter__.return_value = mock_resp_valid

    mock_urlopen.side_effect = [mock_resp_invalid, mock_resp_valid]

    client = ShortCallClient()
    parsed = client.complete_json(
        messages=[{"role": "user", "content": "give json"}], required_keys=["full_key"]
    )

    assert parsed == {"partial": True, "full_key": "some-value"}
    assert mock_urlopen.call_count == 2


@patch("urllib.request.urlopen")
def test_client_retry_once_on_500_then_succeed(mock_urlopen):
    """Verify transient server errors (HTTP 500) trigger a retry and then succeed."""
    mock_error = urllib.error.HTTPError("http://test", 500, "Internal Server Error", {}, None)

    mock_resp_success = MagicMock()
    mock_resp_success.read.return_value = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "Finally worked!"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }
    ).encode("utf-8")
    mock_resp_success.status = 200
    mock_resp_success.__enter__.return_value = mock_resp_success

    # First raises 500, second succeeds
    mock_urlopen.side_effect = [mock_error, mock_resp_success]

    client = ShortCallClient()
    # Patch time.sleep to avoid waiting in tests
    with patch("time.sleep") as mock_sleep:
        res = client.complete([{"role": "user", "content": "hello"}])

    assert res == "Finally worked!"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once_with(1.0)


@patch("urllib.request.urlopen")
def test_client_retry_exhausted_raises_transport_error(mock_urlopen):
    """Verify transient error retries are exhausted and raise LLMTransportError."""
    mock_error = urllib.error.HTTPError("http://test", 503, "Service Unavailable", {}, None)
    mock_urlopen.side_effect = [mock_error, mock_error]

    client = ShortCallClient()
    with patch("time.sleep"), pytest.raises(LLMTransportError) as exc_info:
        client.complete([{"role": "user", "content": "hello"}])

    assert "HTTP transient error 503" in str(exc_info.value)
    assert mock_urlopen.call_count == 2


@patch("urllib.request.urlopen")
def test_client_non_retryable_4xx_raises_client_error(mock_urlopen):
    """Verify non-retryable 4xx client errors (e.g. 401) fail immediately without retrying."""
    mock_error = urllib.error.HTTPError("http://test", 401, "Unauthorized", {}, None)
    mock_urlopen.side_effect = [mock_error, mock_error]

    client = ShortCallClient()
    with pytest.raises(LLMClientError) as exc_info:
        client.complete([{"role": "user", "content": "hello"}])

    assert "HTTP client error 401" in str(exc_info.value)
    # Call count should be exactly 1 because 401 is NOT transient/retryable
    assert mock_urlopen.call_count == 1


@patch("urllib.request.urlopen")
def test_client_respects_budget_and_refuses(mock_urlopen):
    """Verify client complete() consults budget guard and refuses if over budget."""
    config_override = {
        "daily_usd_cap": 0.001,
        "rates": {"gemini-3.6-flash": {"input": 1.0, "output": 1.0}},
    }
    guard = BudgetGuard(config_dict=config_override)

    # Exceed the tiny budget
    guard.record_spend("gemini-3.6-flash", prompt_tokens=10000, completion_tokens=10000)

    client = ShortCallClient(budget_guard=guard)
    with pytest.raises(LLMBudgetExceededError) as exc_info:
        client.complete([{"role": "user", "content": "hello"}])

    assert "spend budget exceeded" in str(exc_info.value)
    # urlopen should not be called at all
    mock_urlopen.assert_not_called()


def test_budget_guard_unreadable_ledger_raises_error(monkeypatch):
    """Verify that if the spend ledger file is unreadable, BudgetGuard fails closed and raises LLMBudgetUnknownError."""
    ledger = _ledger_path()
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    # create the file so it "exists"
    with open(ledger, "w", encoding="utf-8") as f:
        f.write("some content\n")

    original_open = open

    def mock_open(file, *args, **kwargs):
        if str(file) == str(ledger):
            raise OSError("Permission denied / Disk error")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open)

    guard = BudgetGuard()
    with pytest.raises(LLMBudgetUnknownError) as exc_info:
        guard.get_today_spend()

    assert "Budget state is unknown" in str(exc_info.value)

    # Also test that calling check_budget raises it as well
    with pytest.raises(LLMBudgetUnknownError):
        guard.check_budget()


def test_budget_guard_absent_ledger():
    """Verify that if the spend ledger file does not exist, get_today_spend returns 0.0."""
    ledger = _ledger_path()
    if os.path.exists(ledger):
        os.remove(ledger)

    guard = BudgetGuard()
    assert guard.get_today_spend() == 0.0
    # check_budget should pass without raising
    guard.check_budget()


@patch("urllib.request.urlopen")
def test_client_invalid_json_body_raises_error_and_not_retried(mock_urlopen):
    """Verify that a 200 response whose body is not valid JSON raises LLMInvalidResponseError and is not retried."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"Not JSON garbage response"
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    client = ShortCallClient()
    with pytest.raises(LLMInvalidResponseError) as exc_info:
        client.complete([{"role": "user", "content": "hi"}])

    assert "Invalid response format: body is not valid JSON" in str(exc_info.value)
    # Assert urllib.request.urlopen was called exactly once (proving no retry occurred)
    assert mock_urlopen.call_count == 1
