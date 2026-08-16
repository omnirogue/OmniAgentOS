"""Deterministic mock AgentAdapter (FROZEN, Wave 0).

Shared by p02-runner tests, p13-smoke, and the e2e script so runs execute with no
network and no cost. Scripted via AgentInput.metadata["mock"]:

    {"reply": "text",            # output_text (default "mock-ok")
     "json": {...},              # output_json when input.output_schema is set
     "delay_ms": 0,              # sleep before returning
     "fail": false,              # → status=error
     "timeout": false,           # → status=timeout
     "usage": {"turns": 1, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0}}

Side effects deliberately do NOT belong here — external effects are runner `effect`
steps so idempotency is exercised where it lives (contracts/statemachine.md).
"""

from __future__ import annotations

import time
from typing import Any

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HealthStatus,
    ResultStatus,
    estimate_tokens,
)


class MockAdapter:
    name = "mock"
    version = "1.0"

    def run(self, input: AgentInput) -> AgentResult:
        started = time.monotonic()
        spec: dict[str, Any] = dict(input.metadata.get("mock", {}))
        delay_ms = int(spec.get("delay_ms", 0))
        if delay_ms:
            time.sleep(delay_ms / 1000)
        wall_ms = max(1, int((time.monotonic() - started) * 1000))

        usage_spec: dict[str, Any] = dict(spec.get("usage", {}))
        usage = AgentUsage(
            wall_ms=wall_ms,
            turns=usage_spec.get("turns", 1),
            input_tokens=usage_spec.get("input_tokens", estimate_tokens(input.prompt)),
            output_tokens=usage_spec.get("output_tokens", 5),
            cost_usd=usage_spec.get("cost_usd", 0.0),
            estimated=True,
            source="estimator",
        )

        if spec.get("timeout"):
            return AgentResult(status=ResultStatus.TIMEOUT, usage=usage, error="mock timeout")
        if spec.get("fail"):
            return AgentResult(status=ResultStatus.ERROR, usage=usage, error="mock failure")

        reply = str(spec.get("reply", "mock-ok"))
        output_json: dict[str, Any] | None = None
        if input.output_schema is not None:
            output_json = dict(spec.get("json", {"ok": True}))
        return AgentResult(
            status=ResultStatus.OK,
            output_text=reply,
            output_json=output_json,
            session_ref="mock-session",
            usage=usage,
        )

    def cancel(self, session_ref: str) -> bool:
        return True

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, detail="mock", capabilities={"live_runs": True})
