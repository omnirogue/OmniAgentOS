"""Tier2 live probe: a cheap LLM payload flows through the REAL graph runtime.

The LLM only generates the tiny JSON node payload; the verify/completeness
gating exercised here is mechanical (GraphRuntimeService fail-closed policy):
an invalid payload is REFUSED and the valid one lands exactly once.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_PROXY_BASE = "http://localhost:4000/v1"
_PROXY_KEY = "sk-local-litellm-proxy-secure"
_GEMINI_FLASH_PREFERENCE = ("gemini-3.6-flash", "gemini25-flash-lite", "gemini36")


def _proxy_models(timeout: float = 4.0) -> list[str] | None:
    request = urllib.request.Request(
        f"{_PROXY_BASE}/models",
        headers={"Authorization": f"Bearer {_PROXY_KEY}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    return [str(entry.get("id") or "") for entry in payload.get("data", [])]


def test_graph_node_gating_with_live_payload(fh_budget, tmp_path: Path) -> None:
    fh_budget.require_headroom()
    models = _proxy_models()
    if models is None:
        pytest.skip(f"litellm proxy unreachable at {_PROXY_BASE} — cannot generate node payload")
    model = next((m for m in _GEMINI_FLASH_PREFERENCE if m in models), None)
    if model is None:
        pytest.skip(
            f"litellm proxy at {_PROXY_BASE} serves none of {_GEMINI_FLASH_PREFERENCE}"
        )

    from omniagentos.llm.client import ShortCallClient

    client = ShortCallClient(timeout=30.0)
    spend_before = client.budget_guard.get_today_spend()
    payload = client.complete_json(
        [
            {
                "role": "user",
                "content": (
                    'Return ONLY the JSON object {"claim": "<two words>", "score": 0.7} '
                    "with a claim of your choice."
                ),
            }
        ],
        required_keys=["claim", "score"],
        model=model,
        max_tokens=64,
        temperature=0.0,
        purpose="fh-tier2-dag",
    )
    # complete_json may retry internally; the ledger delta is the exact spend.
    fh_budget.record_cost(max(0.0, client.budget_guard.get_today_spend() - spend_before))
    assert isinstance(payload.get("claim"), str) and payload["claim"].strip()

    from omniagentos.graph_runtime.service import GraphRuntimeService

    service = GraphRuntimeService(db_path=str(tmp_path / "graph.db"))
    run = service.start_diamond(title="fh-tier2-dag", completeness_policy="fail_closed")
    run_id = str(run.get("id") or (run.get("run") or {}).get("id"))
    assert run_id and run_id != "None"

    # 1. Invalid payload (missing the required 'finding' output port) is REFUSED.
    with pytest.raises(ValueError, match="missing required output port"):
        service.complete_node(run_id, "fan_a", outputs={"unexpected": payload})
    node = next(n for n in service.get_run(run_id)["nodes"] if n["key"] == "fan_a")
    assert node["status"] in {"ready", "running"}, "refused completion must not advance the node"

    # 2. A blocked downstream node cannot be completed (fail-closed gate).
    with pytest.raises(ValueError, match="cannot complete node"):
        service.complete_node(run_id, "synthesize", outputs={})

    # 3. The valid LLM payload lands — exactly once (idempotent re-complete).
    service.complete_node(run_id, "fan_a", outputs={"finding": payload})
    service.complete_node(run_id, "fan_a", outputs={"finding": {"claim": "other", "score": 0.1}})

    after = service.get_run(run_id)
    node = next(n for n in after["nodes"] if n["key"] == "fan_a")
    assert node["status"] == "completed"
    fan_a_artifacts = [
        a for a in after["artifacts"] if a["node_key"] == "fan_a" and a["port"] == "finding"
    ]
    assert len(fan_a_artifacts) == 1, (
        f"payload must land exactly once; artifacts: {fan_a_artifacts}"
    )
