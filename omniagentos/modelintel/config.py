"""Model Intelligence configuration and brokered provider credentials."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from omniagentos.connectors import broker, load_registry

FUSION_RANKINGS = Path.home() / ".claude" / "fusion" / "model-rankings.json"
FUSION_DIGEST = Path.home() / ".claude" / "fusion" / "model-intel.json"


def _credential(cap_id: str, env_name: str) -> str | None:
    """Resolve a provider credential without exposing an ambient env path."""
    try:
        value = broker.resolve_one_for(load_registry().capability(cap_id), env_name)
    except broker.BrokerDenied:
        return None
    return value or None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def config_path() -> Path:
    override = os.environ.get("OMNIAGENTOS_MODELINTEL_CONFIG")
    if override:
        return Path(override)
    return _repo_root() / "configs" / "modelintel.yaml"


def var_dir() -> Path:
    override = os.environ.get("OMNIAGENTOS_MODELINTEL_DIR")
    if override:
        return Path(override)
    return _repo_root() / "var" / "modelintel"


def registry_path() -> Path:
    return var_dir() / "registry.json"


class RouterConfig(BaseModel):
    model: str = "grok-4.5"
    reasoning_effort: str = "low"
    api_base: str = "https://api.x.ai/v1"
    timeout_seconds: int = 60


class ResearchConfig(BaseModel):
    model: str = "grok-4.5"
    api_base: str = "https://api.x.ai/v1"
    timeout_seconds: int = 240


class DomainSpec(BaseModel):
    key: str
    title: str
    description: str


class BlendComponent(BaseModel):
    benchmark: str
    weight: float


class BenchmarkSpec(BaseModel):
    key: str
    title: str
    metric: str  # "percent" | "elo" | "facts"
    url: str
    fetch: str  # "aider" | "openrouter" | "research"
    description: str


class ModelSpec(BaseModel):
    key: str
    title: str
    provider: str
    lineage: str
    fusion_agents: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    priors: dict[str, float] = Field(default_factory=dict)


class ModelIntelConfig(BaseModel):
    router: RouterConfig = Field(default_factory=RouterConfig)
    router_gemini: RouterConfig = Field(
        default_factory=lambda: RouterConfig(
            model="gemini-3.6-flash",
            api_base="http://localhost:4000/v1",
            timeout_seconds=10,
        )
    )
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    domains: list[DomainSpec] = Field(default_factory=list)
    domain_blend: dict[str, list[BlendComponent]] = Field(default_factory=dict)
    benchmarks: list[BenchmarkSpec] = Field(default_factory=list)
    models: list[ModelSpec] = Field(default_factory=list)

    def benchmark(self, key: str) -> BenchmarkSpec | None:
        return next((b for b in self.benchmarks if b.key == key), None)


def load_config(path: Path | None = None) -> ModelIntelConfig:
    raw = yaml.safe_load((path or config_path()).read_text(encoding="utf-8"))
    return ModelIntelConfig(**raw)


# Trailing tokens that name a CONFIG of a model, not a different model
# ("claude-opus-4-8-thinking" is opus-4.8; "grok-4.5-fast" stays distinct).
_CONFIG_SUFFIXES = {"thinking", "high", "medium", "low", "xhigh", "reasoning", "non-reasoning"}


def normalize_model_name(name: str) -> str:
    """Normalize a published model name for alias matching: lowercase, drop any
    parenthetical suffix ("gpt-5 (high)" -> "gpt-5"), collapse spaces to dashes,
    strip trailing config suffixes ("claude-sonnet-5-high" -> "claude-sonnet-5")."""
    base = name.split("(", 1)[0].strip().lower()
    normalized = "-".join(base.split())
    while True:
        head, _, tail = normalized.rpartition("-")
        if head and tail in _CONFIG_SUFFIXES:
            normalized = head
            continue
        return normalized


def build_alias_index(cfg: ModelIntelConfig) -> dict[str, str]:
    """normalized alias -> model key (model key itself always resolves)."""
    index: dict[str, str] = {}
    for model in cfg.models:
        index[normalize_model_name(model.key)] = model.key
        for alias in model.aliases:
            index[normalize_model_name(alias)] = model.key
    return index


def xai_api_key() -> str | None:
    """Resolve xAI access through its capability-scoped broker boundary."""
    return _credential("xai.generate", "XAI_API_KEY")


def gemini_api_key() -> str | None:
    """Resolve Google AI access through its capability-scoped broker boundary."""
    return _credential("google_ai.generate", "GEMINI_API_KEY")


def aa_api_key() -> str | None:
    """Resolve Artificial Analysis access through the broker, or degrade cleanly."""
    return _credential("artificial_analysis.read", "AA_API_KEY")
