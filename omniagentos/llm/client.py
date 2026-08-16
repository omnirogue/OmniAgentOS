"""LiteLLM short-call client using Python's standard library urllib."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from omniagentos.llm.budget import (
    BudgetGuard,
    LLMClientError,
    LLMInvalidResponseError,
    LLMTransportError,
)


def _clean_and_parse_json(text: str) -> dict[str, Any]:
    """Clean markdown code block fences and parse JSON.

    Args:
        text: The raw response text.

    Returns:
        The parsed JSON dict.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


class ShortCallClient:
    """A standard-library-based client for short, fast structured LLM calls."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 10.0,
        budget_guard: BudgetGuard | None = None,
        retry_transport: bool = True,
    ) -> None:
        """Initialize the ShortCallClient with sensible, configurable defaults.

        Args:
            base_url: Optional overridable base URL (defaults to localhost LiteLLM proxy).
            api_key: Optional bearer api key (defaults to GEMINI_API_KEY env or local proxy secret).
            default_model: Optional default model (defaults to gemini-3.6-flash).
            timeout: Optional HTTP request timeout in seconds (default: 10.0).
            budget_guard: Optional custom BudgetGuard instance.
            retry_transport: Retry once on a transport error (default: True, the
                historical behaviour). Set False when the call sits behind an
                idempotency claim: a read timeout is exactly the case where the
                first request may already have been received and BILLED, and a
                blind re-send then pays twice inside one attempt, where no
                receipt or reservation can undo it. The caller that owns the
                claim decides whether to retry, after classifying the failure.
        """
        self.budget_guard = budget_guard or BudgetGuard()

        self.base_url = (
            base_url or self.budget_guard.config.get("proxy_base_url") or "http://localhost:4000/v1"
        )

        # U-R9: Unbrokered exception (T4.8 owner). This read of GEMINI_API_KEY
        # directly from os.environ bypasses the credential broker. This is the sole
        # sanctioned exception to broker-only resolution in omniagentos/, bounded by
        # the BudgetGuard spend limit (see omniagentos/llm/budget.py). See
        # scheduler/loop_effects.py (the MODEL_COMPLETE declaration) and
        # RESIDUAL-RISKS.md (U-R9). File-level on purpose: a line citation is
        # wrong the first time anyone edits above the line it names.
        self.api_key = (
            api_key or os.environ.get("GEMINI_API_KEY") or "sk-local-litellm-proxy-secure"
        )

        self.default_model = (
            default_model or self.budget_guard.config.get("default_model") or "gemini-3.6-flash"
        )

        self.timeout = timeout
        self.retry_transport = retry_transport

    def _execute_with_retry(self, action_fn: Callable[[], str]) -> str:
        """Execute a function and retry exactly once on transient transport errors.

        Consults the budget guard before every attempt. When
        ``retry_transport`` is False there is exactly one attempt and the
        transport error is raised to the caller unchanged.

        Args:
            action_fn: The no-argument callable that runs the request.

        Returns:
            The text content returned by action_fn.
        """
        last_exc: Exception | None = None
        attempts = 2 if self.retry_transport else 1
        for attempt in range(1, attempts + 1):
            # 1. Consult budget guard BEFORE sending
            self.budget_guard.check_budget()

            try:
                return action_fn()
            except LLMTransportError as exc:
                last_exc = exc
                if attempt == attempts:
                    raise exc
                # Transient error: retry once with short backoff (1.0s)
                time.sleep(1.0)

        if last_exc is not None:
            raise last_exc
        raise LLMTransportError("Retries exhausted without error context.")

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Send a short chat completion request and return the raw text response.

        Args:
            messages: A list of message dicts (e.g., [{"role": "user", "content": "..."}]).
            model: Optional model override. Defaults to default_model.
            temperature: Optional temperature parameter.
            max_tokens: Optional maximum tokens to generate.
            purpose: A tag indicating the caller/purpose for ledger tracking.
            response_format: Optional dict (e.g. {"type": "json_object"}).

        Returns:
            The assistant text content.

        Raises:
            LLMBudgetExceededError: If the daily budget cap is exceeded.
            LLMTransportError: If there's a retryable network or server-side error.
            LLMClientError: If there's a non-retryable 4xx client error (except 429).
        """
        model_to_use = model or self.default_model

        # Build payload
        payload: dict[str, Any] = {
            "model": model_to_use,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        def _execute() -> str:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            url = f"{self.base_url.rstrip('/')}/chat/completions"
            data_bytes = json.dumps(payload).encode("utf-8")

            req = urllib.request.Request(url=url, data=data_bytes, headers=headers, method="POST")

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    body_bytes = response.read()
            except urllib.error.HTTPError as e:
                # Differentiate transient vs client errors
                if e.code == 429 or 500 <= e.code < 600:
                    raise LLMTransportError(f"HTTP transient error {e.code}: {e.reason}") from e
                else:
                    raise LLMClientError(f"HTTP client error {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                raise LLMTransportError(f"Network transport error: {e.reason}") from e
            except (OSError, TimeoutError) as e:
                # Keep a narrow transport safety net for socket/connection/timeout exceptions
                raise LLMTransportError(f"Connection transport error: {str(e)}") from e

            # Decode and parse JSON outside the transport try-except block
            try:
                body = body_bytes.decode("utf-8")
                resp_data = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise LLMInvalidResponseError(
                    f"Invalid response format: body is not valid JSON: {str(e)}"
                ) from e

            # Extract usage token counts if reported
            usage = resp_data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")

            # Extract message content
            try:
                choices = resp_data.get("choices", [])
                assistant_message = choices[0].get("message", {})
                content = assistant_message.get("content", "")
            except (IndexError, AttributeError) as e:
                raise LLMInvalidResponseError(f"Malformed API response structure: {str(e)}") from e

            # Record spend AFTER successful HTTP call
            self.budget_guard.record_spend(
                model=model_to_use,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                purpose=purpose,
            )

            return content

        return self._execute_with_retry(_execute)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
    ) -> dict[str, Any]:
        """Send a JSON-mode chat completion, validating required keys in the response.

        Retries the entire call once on invalid JSON format or missing required keys.
        Note: Since complete_json() retries the whole complete() call once on
        LLMInvalidResponseError, and complete() itself retries once on transport error,
        the worst-case scenario can result in up to 4 HTTP calls.

        Args:
            messages: A list of message dicts.
            required_keys: List of string keys that must be present in the returned JSON.
            model: Optional model override.
            temperature: Optional temperature parameter.
            max_tokens: Optional maximum tokens to generate.
            purpose: A tag indicating the caller/purpose for ledger tracking.

        Returns:
            The parsed and validated JSON dict.

        Raises:
            LLMBudgetExceededError: If the daily budget cap is exceeded.
            LLMTransportError: If there's a retryable network or server-side error.
            LLMClientError: If there's a non-retryable 4xx client error.
            LLMInvalidResponseError: If JSON parsing or validation fails after retry.
        """
        attempt = 1
        while True:
            try:
                content = self.complete(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    purpose=purpose,
                    response_format={"type": "json_object"},
                )

                # Parse JSON safely, ignoring markdown block fences
                try:
                    parsed = _clean_and_parse_json(content)
                except Exception as e:
                    raise LLMInvalidResponseError(
                        f"Unparseable JSON returned by model: {str(e)}\nRaw response: {content}"
                    ) from e

                # Validate schema keys
                missing = [key for key in required_keys if key not in parsed]
                if missing:
                    raise LLMInvalidResponseError(
                        f"JSON response missing required keys: {missing}\nParsed object: {parsed}"
                    )

                return parsed

            except LLMInvalidResponseError as exc:
                if attempt >= 2:
                    raise exc
                attempt += 1
                time.sleep(1.0)
