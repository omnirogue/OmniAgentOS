"""Deterministic loopback-only OpenAI contract server for cost-edge tests.

PC catalog (P1-COST-EDGE):
  PC1  — exact cost success (0.01144063)
  PC2  — missing cost field (unknown)
  PC3  — billed HTTP failure that still reports exact cost
  PC4  — served-model identity (strict by default; mutation flag swaps)
  PC7  — delayed response (client must bound)
  PC8  — first 503 then success
  PC11 — exact zero cost
  PC12 — invalid cost string
  PC13 — large exact cost (nano-safe round-trip)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

EXACT_COST_DECIMAL = "0.01144063"
EXACT_COST_TEXT = EXACT_COST_DECIMAL
EXACT_COST_FLOAT = 0.01144063
EXACT_COST = EXACT_COST_FLOAT
EXACT_COST_NANOS = 11_440_630

LARGE_COST_DECIMAL = "999.000000001"
LARGE_COST_NANOS = 999_000_000_001

SCENARIO_IDS = ("PC1", "PC2", "PC3", "PC4", "PC7", "PC8", "PC11", "PC12", "PC13")
ALL_SCENARIOS = SCENARIO_IDS
DEFAULT_SERVED_MODEL = "x-ai/grok-4.5"
PC3_BILLED_MODEL = "x-ai/grok-4.5"
PC4_EXPECTED_MODEL = "x-ai/grok-4.5"
PC4_WRONG_MODEL = "x-ai/WRONG-MODEL"
# Compatibility name used by the PC11 packet: zero cost is valid only for the
# model the deterministic server actually serves.
PC11_ALLOWED_MODEL = DEFAULT_SERVED_MODEL
# Alias kept for older callers of the packet name.
PC4_ALTERNATE_MODEL = PC4_WRONG_MODEL


class ContractServerState:
    """Thread-safe scenario state and request history."""

    def __init__(self, scenario: str | None = None) -> None:
        if scenario is not None and scenario.upper() not in SCENARIO_IDS:
            raise ValueError(f"unknown contract scenario: {scenario}")
        self.scenario = scenario.upper() if scenario else None
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.pc7_delay_s = 1.0
        self.pc8_hits = 0
        # Negative-mutation surface: when True, PC4 serves a different model id.
        # Production default is False — strict served-model identity.
        self.accept_wrong_model: bool = False

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(entry)

    def count_pc8(self) -> int:
        with self.lock:
            self.pc8_hits += 1
            return self.pc8_hits


class _ContractHandler(BaseHTTPRequestHandler):
    server_version = "OmniCostContract/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    @property
    def state(self) -> ContractServerState:
        return self.server.contract_state  # type: ignore[attr-defined,no-any-return]

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Expected when PC7 intentionally outlives the client's deadline.
            pass

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _scenario(self, body: dict[str, Any]) -> str:
        for header in ("X-Omni-Contract-Scenario", "X-Contract-Scenario"):
            selected = (self.headers.get(header) or "").strip().upper()
            if selected in SCENARIO_IDS:
                return selected
        if self.state.scenario:
            return self.state.scenario
        model = str(body.get("model") or "").upper()
        for scenario in SCENARIO_IDS:
            if scenario in model:
                return scenario
        return "PC1"

    @staticmethod
    def _success(
        model: str,
        *,
        cost: str | float | None = EXACT_COST_DECIMAL,
        omit_cost: bool = False,
    ) -> dict[str, Any]:
        usage: dict[str, Any] = {
            "prompt_tokens": 3,
            "completion_tokens": 5,
            "total_tokens": 8,
        }
        if not omit_cost and cost is not None:
            usage["cost"] = cost
        return {
            "id": "chatcmpl-cost-contract",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"contract-ok:{model}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path in {"/", "/health", "/v1/health"}:
            self._send_json(200, {"ok": True, "scenarios": list(SCENARIO_IDS)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path not in {"/chat/completions", "/v1/chat/completions"}:
            self._send_json(404, {"error": "not found"})
            return

        body = self._read_json()
        scenario = self._scenario(body)
        requested_model = str(body.get("model") or "") or DEFAULT_SERVED_MODEL
        self.state.record(
            {"scenario": scenario, "model": requested_model, "path": path}
        )

        if scenario == "PC2":
            # Missing cost field — unknown, not free.
            self._send_json(200, self._success(requested_model, omit_cost=True))
            return
        if scenario == "PC3":
            self._send_json(
                402,
                {
                    "id": "chatcmpl-billed-failure",
                    "error": {"message": "billed provider failure", "code": 402},
                    "model": PC3_BILLED_MODEL,
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                        "cost": EXACT_COST_DECIMAL,
                    },
                },
            )
            return
        if scenario == "PC4":
            served = (
                PC4_WRONG_MODEL if self.state.accept_wrong_model else requested_model
            )
            self._send_json(200, self._success(served))
            return
        if scenario == "PC7":
            time.sleep(self.state.pc7_delay_s)
            self._send_json(200, self._success(requested_model))
            return
        if scenario == "PC8":
            if self.state.count_pc8() == 1:
                self._send_json(503, {"error": {"message": "retryable failure"}})
            else:
                self._send_json(200, self._success(requested_model))
            return
        if scenario == "PC11":
            self._send_json(200, self._success(requested_model, cost=0.0))
            return
        if scenario == "PC12":
            self._send_json(200, self._success(requested_model, cost="not-a-cost"))
            return
        if scenario == "PC13":
            self._send_json(200, self._success(requested_model, cost=LARGE_COST_DECIMAL))
            return
        # PC1 (default): exact cost success.
        self._send_json(200, self._success(requested_model, cost=EXACT_COST_DECIMAL))


def start_contract_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    scenario: str | None = None,
) -> tuple[ThreadingHTTPServer, str, ContractServerState]:
    """Start a daemon contract server bound to IPv4 loopback only."""

    if host != "127.0.0.1":
        raise ValueError("contract server is loopback-only (127.0.0.1)")
    state = ContractServerState(scenario)
    server = ThreadingHTTPServer((host, port), _ContractHandler)
    server.contract_state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    server.contract_thread = thread  # type: ignore[attr-defined]
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    return server, base_url, state


def stop_contract_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
    thread = getattr(server, "contract_thread", None)
    if isinstance(thread, threading.Thread):
        thread.join(timeout=5.0)


class OpenRouterContractServer:
    """Context-manager facade retained for callers that prefer a class API."""

    def __init__(self, *, scenario: str | None = None) -> None:
        self.scenario = scenario
        self._server: ThreadingHTTPServer | None = None
        self.base_url = ""
        self.state: ContractServerState | None = None

    def start(self) -> None:
        if self._server is None:
            self._server, self.base_url, self.state = start_contract_server(
                scenario=self.scenario
            )

    def stop(self) -> None:
        if self._server is not None:
            stop_contract_server(self._server)
            self._server = None

    def __enter__(self) -> OpenRouterContractServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = [
    "ALL_SCENARIOS",
    "ContractServerState",
    "DEFAULT_SERVED_MODEL",
    "EXACT_COST",
    "EXACT_COST_DECIMAL",
    "EXACT_COST_FLOAT",
    "EXACT_COST_NANOS",
    "EXACT_COST_TEXT",
    "LARGE_COST_DECIMAL",
    "LARGE_COST_NANOS",
    "OpenRouterContractServer",
    "PC3_BILLED_MODEL",
    "PC4_ALTERNATE_MODEL",
    "PC4_EXPECTED_MODEL",
    "PC4_WRONG_MODEL",
    "PC11_ALLOWED_MODEL",
    "SCENARIO_IDS",
    "start_contract_server",
    "stop_contract_server",
]
