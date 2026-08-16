"""Tier2 live probe: dispatch Gate 2 verdicts served by a REAL cheap model.

Uses the documented per-call ``chain=`` override of
``omniagentos.intake.fallback.run_with_fallback`` (see gate.py:_default_llm_fn)
pinned to the cheapest reachable rung: the gemini-lite api rung on the local
LiteLLM proxy when it serves a flash-lite model, else the haiku CLI rung.
The semantic router leg (Gate 1) is force-disabled via the injectable
``router_factory`` seam so no network embedding model is ever needed.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]

_PROXY_BASE = "http://localhost:4000/v1"
_PROXY_KEY = "sk-local-litellm-proxy-secure"

# Canned briefs crafted to fall through Gate 0 (no risk/sweep phrasing, no
# bounded solo-fast verb verdict) so the LLM gate genuinely decides.
_BRIEFS = (
    "The nightly reconciliation report and the ledger snapshot disagree about "
    "totals; investigate why they diverge and describe the safest way to bring "
    "them back in line.",
    "Users occasionally see stale numbers on the analytics dashboard after a "
    "cache refresh; figure out whether the caching layer or the query layer is "
    "responsible and propose a remedy.",
)

# Conservative per-call ceiling for a <=64-token flash-lite verdict via the
# api rung (run_with_fallback does not surface provider usage to the caller;
# actual cost is ~1e-4 USD at flash-lite rates).
_API_CALL_COST_CEILING_USD = 0.001


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


class _DisabledRouter:
    """Gate 1 seam: classify() abstains so decide() falls through to Gate 2."""

    def classify(self, brief: str) -> tuple[None, float]:
        del brief
        return None, 0.0


def test_gate2_verdicts_with_real_cheap_model(fh_budget) -> None:
    fh_budget.require_headroom()

    from omniagentos.contracts import HarnessType
    from omniagentos.dispatch import gate as gate_mod
    from omniagentos.intake.fallback import LITE_PLANNER_MODEL, run_with_fallback

    served = _proxy_models()
    lite_model = None
    if served is not None:
        for candidate in (LITE_PLANNER_MODEL, "gemini25-flash-lite", "gemini-3.5-flash-lite"):
            if candidate in served:
                lite_model = candidate
                break

    if lite_model is not None:
        # Cheapest rung: the gemini-lite api rung, pinned via the explicit
        # (name, harness, model) chain-tuple idiom (fallback._resolve_chain).
        chain: list[Any] = [("gemini-lite-api", "api-litellm", lite_model)]
        via_cli = False
    elif shutil.which("claude") is not None:
        chain = [("haiku", HarnessType.CLI_CLAUDE, "haiku")]
        via_cli = True
    else:
        pytest.skip(
            f"no cheap Gate-2 rung available: litellm proxy at {_PROXY_BASE} "
            f"{'unreachable' if served is None else 'serves no flash-lite model'} "
            "and claude CLI absent from PATH"
        )

    raw_verdicts: list[dict[str, Any] | None] = []

    def _llm_fn(brief: str) -> dict[str, Any] | None:
        if via_cli:
            fh_budget.require_headroom(cli=True)
            fh_budget.record_cli_call()
        result = run_with_fallback(
            gate_mod._LLM_PROMPT.replace("{brief}", brief),
            gate_mod._LLM_SCHEMA,
            effort="low",
            max_turns=1,
            wall_ms=45_000,
            chain=chain,
        )
        if not via_cli:
            fh_budget.record_cost(_API_CALL_COST_CEILING_USD)
        raw_verdicts.append(result)
        return result

    decisions = []
    for brief in _BRIEFS:
        decision = gate_mod.decide(
            brief,
            router_factory=lambda config: _DisabledRouter(),
            llm_fn=_llm_fn,
        )
        decisions.append(decision)

    # The LLM gate must have been genuinely consulted for BOTH briefs.
    assert len(raw_verdicts) == 2, (
        f"Gate 0/1 short-circuited: llm_fn saw {len(raw_verdicts)} of 2 briefs "
        f"(decisions: {[(d.gate, d.decision, d.reason) for d in decisions]})"
    )
    for raw, decision, brief in zip(raw_verdicts, decisions, _BRIEFS, strict=True):
        # Structured verdict parsed from the real cheap model.
        assert isinstance(raw, dict), (
            f"cheap rung {chain[0]} returned no structured verdict for {brief!r}"
        )
        assert set(raw) >= {"decision", "confidence", "reason"}, f"malformed verdict: {raw}"
        assert raw["decision"] in gate_mod._DECISIONS, f"unknown decision: {raw}"
        # decide() folded it into a well-formed GateDecision.
        assert isinstance(decision, gate_mod.GateDecision)
        assert decision.decision in gate_mod._DECISIONS
        assert decision.gate in {"llm", "fallthrough"}, (
            f"verdict did not come from Gate 2: gate={decision.gate} reason={decision.reason}"
        )
