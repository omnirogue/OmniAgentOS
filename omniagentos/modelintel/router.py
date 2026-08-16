"""Optional LLM router — Grok 4.5 at reasoning_effort=low picks the Fusion
agent for a task from the live capability digest.

Contract with callers (route-task.py --llm-task, OmniAgentOS orchestration):
- route() ALWAYS returns a verdict; if the xAI call fails, times out, or
  returns an invalid pick, the deterministic fallback scorer (a port of
  route-task.py's eligible()/fitScore()) decides instead and the verdict says
  `router: "mechanical-fallback"` with the failure reason attached.
- A pick is only valid if the agent id exists in ~/.claude/fusion/
  model-rankings.json AND is currently available; effort is clamped to the
  agent's maxReasoning. The LLM proposes, the rankings file disposes.

Both router calls speak HTTP to a model, so BOTH are gated by
`omniagentos/routing/api_policy.py` before a request is constructed: the router
model is CONFIG (`configs/modelintel.yaml` router / router_gemini), and a config
edit pointing it at a claude or gpt-* id must never put that lineage on a paid
API path. A denied model is refused before any request object exists and the
call degrades to the mechanical scorer with a `POLICY DENY` fallback_reason —
loud in the verdict and the log, but still honoring the never-raise contract
above (callers dispatch on the verdict; a config typo must not take routing
down, and no request is sent either way).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import requests
from pydantic import BaseModel, Field

from omniagentos.modelintel.config import (
    FUSION_DIGEST,
    FUSION_RANKINGS,
    ModelIntelConfig,
    gemini_api_key,
    load_config,
    xai_api_key,
)
from omniagentos.routing.api_policy import api_route_denial

LOG = logging.getLogger(__name__)

MODES = ("superfast", "ultrafast", "fusionbuild", "ultrabuild")
DIFFICULTIES = ("trivial", "easy", "moderate", "hard", "architectural")
EFFORTS = ("low", "medium", "high", "xhigh")

# Superfast is deliberately more opinionated than the older latency-weighted
# ultrafast profile.  If a fast-lane model clears the task's capability and
# reasoning floors, only fast-lane models compete.  If none clears the floor,
# the full auto-route pool is restored so quality wins over the speed target.
# The explicit registry flag is authoritative; the ids are a compatibility
# default for rankings files written before the flag was introduced.
DEFAULT_FAST_LANE = {"luna-coder", "terra-coder"}

# Mirror of route-task.py's mode weights — keep the two in sync by hand; the
# fallback must rank identically to the mechanical router it stands in for.
FALLBACK_WEIGHTS = {
    "superfast": {"wL": 0.50, "wQ": 0.15, "wT": 0.15, "wH": 0.10, "wC": 0.05, "wR": 0.05},
    "ultrafast": {"wL": 0.50, "wQ": 0.15, "wT": 0.15, "wH": 0.10, "wC": 0.05, "wR": 0.05},
    "fusionbuild": {"wL": 0.20, "wQ": 0.30, "wT": 0.20, "wH": 0.15, "wC": 0.10, "wR": 0.05},
    "ultrabuild": {"wL": 0.05, "wQ": 0.40, "wT": 0.20, "wH": 0.25, "wC": 0.05, "wR": 0.05},
}


class RouteVerdict(BaseModel):
    pick: str | None
    model: str | None = None
    effort: str = "medium"
    why: str = ""
    alternatives: list[str] = Field(default_factory=list)
    router: str = "grok"  # "grok" | "mechanical-fallback"
    lane: str = "all-capable"  # "fast" | "quality-escalation" | "all-capable"
    fallback_reason: str | None = None


def _policy_denial(model: str, api_base: str) -> str | None:
    """None when `model` may be called over `api_base`; else the deny reason.

    THE gate for this module: every HTTP model call goes through it BEFORE a
    request is constructed, because the model id is config
    (configs/modelintel.yaml) and a config edit must never be able to put a
    claude/gpt-* id on a paid API path.
    """
    return api_route_denial(model, api_base=api_base)


def _load_json(path: Any) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _rankings_agents() -> dict[str, dict[str, Any]]:
    rankings = _load_json(FUSION_RANKINGS) or {}
    return {a["id"]: a for a in rankings.get("agents", []) if a.get("id")}


def _clamp_effort(effort: str, agent: dict[str, Any]) -> str:
    if effort not in EFFORTS:
        effort = "medium"
    max_effort = agent.get("maxReasoning", "xhigh")
    max_idx = EFFORTS.index(max_effort) if max_effort in EFFORTS else len(EFFORTS) - 1
    return EFFORTS[min(EFFORTS.index(effort), max_idx)]


def _is_fast_lane(agent_id: str, agent: dict[str, Any]) -> bool:
    flag = agent.get("fastLane")
    return bool(flag) if flag is not None else agent_id in DEFAULT_FAST_LANE


def _lane_for_pick(mode: str, agent_id: str | None, agent: dict[str, Any] | None) -> str:
    if mode != "superfast":
        return "all-capable"
    if agent_id and agent and _is_fast_lane(agent_id, agent):
        return "fast"
    return "quality-escalation"


def _eligible_agents(
    mode: str,
    difficulty: str,
    reasoning: str,
    agents: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    eligible: dict[str, dict[str, Any]] = {}
    for agent_id, agent in agents.items():
        if (
            agent.get("role") != "coder"
            or not agent.get("available")
            or not agent.get("autoRoute", True)
        ):
            continue
        tier = agent.get("capabilityTier", "moderate")
        max_effort = agent.get("maxReasoning", "medium")
        if tier not in DIFFICULTIES or max_effort not in EFFORTS:
            continue
        if DIFFICULTIES.index(tier) < DIFFICULTIES.index(difficulty):
            continue
        if EFFORTS.index(max_effort) < EFFORTS.index(reasoning):
            continue
        eligible[agent_id] = agent

    if mode == "superfast":
        fast_lane = {
            agent_id: agent
            for agent_id, agent in eligible.items()
            if _is_fast_lane(agent_id, agent)
        }
        if fast_lane:
            return fast_lane
    return eligible


def _mechanical(
    mode: str, difficulty: str, reasoning: str, agents: dict[str, dict[str, Any]], reason: str
) -> RouteVerdict:
    """Deterministic fallback — same formula as route-task.py fit_score()."""
    weights = FALLBACK_WEIGHTS[mode]
    scored: list[tuple[float, str, dict[str, Any]]] = []
    eligible = _eligible_agents(mode, difficulty, reasoning, agents)
    for agent_id, agent in eligible.items():
        latency = agent.get("warmLatencyMs")
        speed = 1000.0 / latency if latency and latency > 0 else 0.1
        success = agent.get("successRate")
        score = (
            weights["wL"] * speed
            + weights["wQ"] * agent.get("codingScore", 0.5)
            + weights["wT"] * agent.get("toolUseScore", 0.5)
            + weights["wH"] * (0.5 if success is None else success)
            - weights["wC"] * (1.0 - agent.get("costScore", 0.5))
            - weights["wR"] * (agent.get("rateLimitPressure", 0.0) or 0.0)
        )
        scored.append((score, agent_id, agent))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return RouteVerdict(
            pick=None,
            router="mechanical-fallback",
            lane=_lane_for_pick(mode, None, None),
            fallback_reason=reason,
            why="no eligible agent for this difficulty/reasoning",
        )
    _, best_id, best = scored[0]
    return RouteVerdict(
        pick=best_id,
        model=best.get("model"),
        effort=_clamp_effort(reasoning, best),
        why=f"mechanical fit-score winner for {mode}/{difficulty}",
        alternatives=[agent_id for _, agent_id, _ in scored[1:4]],
        router="mechanical-fallback",
        lane=_lane_for_pick(mode, best_id, best),
        fallback_reason=reason,
    )


def _routing_prompt(digest: dict[str, Any], agents: dict[str, dict[str, Any]]) -> str:
    launchable = []
    for entry in digest.get("agents", []):
        ranked = agents.get(entry["id"])
        if not ranked or not ranked.get("available"):
            continue
        launchable.append(
            {
                "id": entry["id"],
                "model": entry.get("model"),
                "lineage": entry.get("lineage"),
                "capabilityTier": ranked.get("capabilityTier"),
                "maxReasoning": ranked.get("maxReasoning"),
                "fastLane": _is_fast_lane(entry["id"], ranked),
                "latencyMs": entry.get("latencyMs"),
                "promptUsdPerM": entry.get("promptUsdPerM"),
                "scores": entry.get("scores"),
            }
        )
    return (
        "You are the routing brain of a multi-model coding orchestrator. Pick the "
        "single best agent for the task.\n\n"
        f"AGENTS (only these ids are valid picks):\n{json.dumps(launchable, indent=1)}\n\n"
        f"DOMAIN KEY: {json.dumps(digest.get('domains', {}))}\n"
        f"CURRENT BEST BY DOMAIN: {json.dumps(digest.get('topByDomain', {}))}\n\n"
        "Mode guidance — superfast: use the supplied fast-lane candidates because "
        "they already cleared the capability floor; ultrafast: favor speed if quality "
        "suffices; fusionbuild: "
        "balance quality and throughput; ultrabuild: maximize quality/reliability, "
        "latency is nearly irrelevant.\n"
        "Effort ladder: low < medium < high < xhigh; never exceed the agent's "
        "maxReasoning; spend effort on hard/architectural work, not trivia.\n\n"
        'Respond with ONLY a JSON object: {"pick": "<agent id>", '
        '"effort": "low|medium|high|xhigh", "why": "<one sentence>", '
        '"alternatives": ["<agent id>", ...]}'
    )


def route(
    task: str,
    mode: str = "fusionbuild",
    difficulty: str = "moderate",
    reasoning: str = "medium",
    cfg: ModelIntelConfig | None = None,
    force_mechanical: bool = False,
) -> RouteVerdict:
    if mode not in MODES:
        mode = "fusionbuild"
    if difficulty not in DIFFICULTIES:
        difficulty = "moderate"
    if reasoning not in EFFORTS:
        reasoning = "medium"
    agents = _rankings_agents()
    if not agents:
        return RouteVerdict(
            pick=None,
            router="mechanical-fallback",
            fallback_reason="model-rankings.json missing — run refresh-rankings.sh",
            why="no agent roster available",
        )
    if force_mechanical:
        return _mechanical(mode, difficulty, reasoning, agents, "forced mechanical")

    eligible = _eligible_agents(mode, difficulty, reasoning, agents)
    if not eligible:
        return _mechanical(mode, difficulty, reasoning, agents, "no eligible auto-route agent")

    cfg = cfg or load_config()

    # 1. Resolve router mode flags
    router_mode = os.environ.get("OMNIAGENTOS_ROUTER_LLM", "default").strip().lower()
    shadow_enabled = os.environ.get("OMNIAGENTOS_ROUTER_SHADOW", "0").strip() == "1"

    # Helper function to call Grok/incumbent router
    def _call_incumbent() -> RouteVerdict:
        denial = _policy_denial(cfg.router.model, cfg.router.api_base)
        if denial:
            LOG.error("router model %r refused by api policy: %s", cfg.router.model, denial)
            return _mechanical(mode, difficulty, reasoning, agents, denial)
        api_key = xai_api_key()
        if not api_key:
            return _mechanical(mode, difficulty, reasoning, agents, "no XAI_API_KEY")
        digest = _load_json(FUSION_DIGEST)
        if not digest:
            return _mechanical(
                mode,
                difficulty,
                reasoning,
                agents,
                "model-intel.json missing — run modelintel update",
            )
        try:
            resp = requests.post(
                f"{cfg.router.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": cfg.router.model,
                    "reasoning_effort": cfg.router.reasoning_effort,
                    "messages": [
                        {"role": "system", "content": _routing_prompt(digest, eligible)},
                        {
                            "role": "user",
                            "content": (
                                f"MODE: {mode}\nDIFFICULTY HINT: {difficulty}\n"
                                f"REASONING HINT: {reasoning}\nTASK:\n{task}"
                            ),
                        },
                    ],
                },
                timeout=cfg.router.timeout_seconds,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            start, end = content.find("{"), content.rfind("}")
            verdict = json.loads(content[start : end + 1])
            pick = verdict.get("pick")
            agent = eligible.get(pick)
            if not agent:
                raise ValueError(f"invalid pick {pick!r}")
            alternatives = [
                a for a in verdict.get("alternatives", []) if a != pick and a in eligible
            ]
            return RouteVerdict(
                pick=pick,
                model=agent.get("model"),
                effort=_clamp_effort(str(verdict.get("effort", reasoning)), agent),
                why=str(verdict.get("why", "")),
                alternatives=alternatives[:3],
                router="grok",
                lane=_lane_for_pick(mode, pick, agent),
            )
        except Exception as exc:  # noqa: BLE001 - fallback is the contract
            return _mechanical(mode, difficulty, reasoning, agents, f"grok route failed: {exc}")

    # Helper function to call Gemini/flash router
    def _call_gemini() -> RouteVerdict:
        denial = _policy_denial(cfg.router_gemini.model, cfg.router_gemini.api_base)
        if denial:
            LOG.error(
                "router_gemini model %r refused by api policy: %s",
                cfg.router_gemini.model,
                denial,
            )
            return _mechanical(mode, difficulty, reasoning, agents, denial)
        api_key = gemini_api_key()
        # If no gemini key is found, fall back to "sk-local-litellm-proxy-secure" dummy key,
        # since local LiteLLM proxy already has the real key in connections.env.
        if not api_key:
            api_key = "sk-local-litellm-proxy-secure"
        digest = _load_json(FUSION_DIGEST)
        if not digest:
            return _mechanical(
                mode,
                difficulty,
                reasoning,
                agents,
                "model-intel.json missing — run modelintel update",
            )
        try:
            resp = requests.post(
                f"{cfg.router_gemini.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": cfg.router_gemini.model,
                    "messages": [
                        {"role": "system", "content": _routing_prompt(digest, eligible)},
                        {
                            "role": "user",
                            "content": (
                                f"MODE: {mode}\nDIFFICULTY HINT: {difficulty}\n"
                                f"REASONING HINT: {reasoning}\nTASK:\n{task}"
                            ),
                        },
                    ],
                },
                timeout=cfg.router_gemini.timeout_seconds,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            start, end = content.find("{"), content.rfind("}")
            verdict = json.loads(content[start : end + 1])
            pick = verdict.get("pick")
            agent = eligible.get(pick)
            if not agent:
                raise ValueError(f"invalid pick {pick!r}")
            alternatives = [
                a for a in verdict.get("alternatives", []) if a != pick and a in eligible
            ]
            return RouteVerdict(
                pick=pick,
                model=agent.get("model"),
                effort=_clamp_effort(str(verdict.get("effort", reasoning)), agent),
                why=str(verdict.get("why", "")),
                alternatives=alternatives[:3],
                router="gemini-flash",
                lane=_lane_for_pick(mode, pick, agent),
            )
        except Exception as exc:  # noqa: BLE001 - fallback is the contract
            return _mechanical(mode, difficulty, reasoning, agents, f"gemini route failed: {exc}")

    # Executive decision block:

    # 2. Shadow mode: call BOTH, log results, return incumbent
    if shadow_enabled:
        t0 = time.time()
        incumbent_verdict = _call_incumbent()
        t1 = time.time()
        incumbent_ms = int((t1 - t0) * 1000)

        t2 = time.time()
        try:
            gemini_verdict = _call_gemini()
        except Exception as exc:
            gemini_verdict = RouteVerdict(
                pick=None,
                router="gemini-flash-error",
                fallback_reason=f"shadow call exception: {exc}",
            )
        t3 = time.time()
        gemini_ms = int((t3 - t2) * 1000)

        # Log comparison to var/modelintel/router_shadow.jsonl
        try:
            import hashlib

            from omniagentos.modelintel.config import var_dir

            input_hash = hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]
            log_data = {
                "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "input_hash": input_hash,
                "mode": mode,
                "difficulty": difficulty,
                "reasoning": reasoning,
                "incumbent_decision": incumbent_verdict.model_dump(),
                "incumbent_latency_ms": incumbent_ms,
                "flash_decision": gemini_verdict.model_dump(),
                "flash_latency_ms": gemini_ms,
            }
            shadow_file = var_dir() / "router_shadow.jsonl"
            shadow_file.parent.mkdir(parents=True, exist_ok=True)
            with open(shadow_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_data) + "\n")
        except Exception:  # noqa: BLE001
            LOG.warning("router shadow logging failed", exc_info=True)

        return incumbent_verdict

    # 3. Dedicated Gemini mode
    elif router_mode == "gemini-flash":
        return _call_gemini()

    # 4. Default mode (current Grok router)
    else:
        return _call_incumbent()
