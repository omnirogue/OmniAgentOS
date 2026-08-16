"""Grok live-web research sweep — fills the benchmarks that have no stable API.

Uses the xAI Agent Tools API (POST /v1/responses with a server-side web_search
tool; the old `search_parameters` Live Search is deprecated). One call per day
asks Grok to pull the current numbers for every `fetch: research` benchmark in
configs/modelintel.yaml and any notable new model releases. Findings merge into
the registry with provenance "grok-research" and confidence low — they inform
scores but a failed sweep never blocks the daily update.
"""

from __future__ import annotations

import json
from typing import Any

import requests
from pydantic import BaseModel, Field

from omniagentos.contracts import utc_now_iso
from omniagentos.modelintel.config import ModelIntelConfig
from omniagentos.modelintel.sources import BenchmarkRow, _cache_raw
from omniagentos.routing.api_policy import api_route_denial


class ReleaseNote(BaseModel):
    model_name: str
    note: str
    source_url: str | None = None


class ResearchResult(BaseModel):
    fetched_at: str
    ok: bool
    error: str | None = None
    rows: list[BenchmarkRow] = Field(default_factory=list)
    releases: list[ReleaseNote] = Field(default_factory=list)


def _build_prompt(cfg: ModelIntelConfig) -> str:
    research_benchmarks = [b for b in cfg.benchmarks if b.fetch == "research"]
    bench_lines = "\n".join(
        f"- {b.key}: {b.title} ({b.url}) — metric: {b.metric}" for b in research_benchmarks
    )
    tracked = ", ".join(m.title for m in cfg.models)
    return (
        "You maintain a model-capability registry for a coding-agent orchestrator. "
        "Search the web for the CURRENT leaderboard standings of these benchmarks:\n"
        f"{bench_lines}\n\n"
        f"Tracked models (report these whenever the board lists them): {tracked}. "
        "Also report the overall top 5 of each board even if untracked, and any "
        "notable coding-model releases or benchmark shake-ups from the last 14 days.\n\n"
        "Respond with ONLY a JSON object, no prose, shaped exactly like:\n"
        "{\n"
        '  "findings": [{"benchmark": "<benchmark key from the list above>",\n'
        '                "model_name": "<name as published>", "score": <number>,\n'
        '                "metric": "percent"|"elo", "source_url": "<url>",\n'
        '                "note": "<optional context>"}],\n'
        '  "releases": [{"model_name": "<name>", "note": "<one line>", "source_url": "<url>"}]\n'
        "}\n"
        "Rules: scores must be numbers you actually found (no guesses); use the "
        "benchmark keys verbatim; omit a benchmark entirely if you cannot verify "
        "current numbers."
    )


def _extract_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output", []):
        for chunk in item.get("content") or []:
            if chunk.get("type") == "output_text" and chunk.get("text"):
                parts.append(chunk["text"])
    return "\n".join(parts)


def _extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in research response")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("research response JSON is not an object")
    return parsed


def sweep(cfg: ModelIntelConfig, api_key: str) -> ResearchResult:
    fetched_at = utc_now_iso()
    # Same gate as the router: `research.model` is CONFIG, and a config edit
    # must never put a claude/gpt-* id on a paid API path. Refused before the
    # request exists; a failed sweep is already a non-blocking outcome here.
    denial = api_route_denial(cfg.research.model, api_base=cfg.research.api_base)
    if denial:
        return ResearchResult(fetched_at=fetched_at, ok=False, error=denial)
    try:
        resp = requests.post(
            f"{cfg.research.api_base}/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": cfg.research.model,
                "input": _build_prompt(cfg),
                "tools": [{"type": "web_search"}],
            },
            timeout=cfg.research.timeout_seconds,
        )
        resp.raise_for_status()
        _cache_raw("grok-research", resp.text, "json")
        text = _extract_output_text(resp.json())
        data = _extract_json(text)
        valid_keys = {b.key for b in cfg.benchmarks}
        rows = []
        for finding in data.get("findings", []):
            if finding.get("benchmark") not in valid_keys:
                continue
            if finding.get("score") is None or not finding.get("model_name"):
                continue
            rows.append(
                BenchmarkRow(
                    benchmark=finding["benchmark"],
                    model_name=str(finding["model_name"]),
                    score=float(finding["score"]),
                    metric=str(finding.get("metric") or "percent"),
                    source_url=str(finding.get("source_url") or ""),
                    note=finding.get("note"),
                )
            )
        releases = [
            ReleaseNote(
                model_name=str(r.get("model_name", "")),
                note=str(r.get("note", "")),
                source_url=r.get("source_url"),
            )
            for r in data.get("releases", [])
            if r.get("model_name")
        ]
        return ResearchResult(fetched_at=fetched_at, ok=True, rows=rows, releases=releases)
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        return ResearchResult(fetched_at=fetched_at, ok=False, error=str(exc))
