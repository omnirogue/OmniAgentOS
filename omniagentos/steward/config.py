"""Validated configuration for the Horizon 4 Steward."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BriefingConfig(BaseModel):
    hour: int = 7
    minute: int = 30
    deliver_email: str | None = None
    deliver_slack: bool = True
    voice_provider: str | None = "elevenlabs"


class AlertsConfig(BaseModel):
    roas_floor: float = 1.0
    spend_spike_pct: float = 50.0
    # Spend circuit breaker (steward/alerts/rules.spend_spike_intraday):
    # optional absolute daily USD cap that fires an intraday, no-baseline-
    # required breaker trip. None (default) disables the intraday path;
    # spend_spike's relative/baseline rule is unaffected either way.
    spend_spike_absolute_cap_usd: float | None = None
    payment_failure_burst: int = 3
    vip_senders: list[str] = Field(default_factory=list)
    cooldown_minutes: int = 240
    urgent_patterns: list[str] = Field(default_factory=lambda: ["urgent", "asap", "immediately"])
    # H10/PROD-001 revenue-crash alert (rules.revenue_drop): floor and trailing-
    # baseline percentage-drop thresholds for the increase-revenue goal's
    # net_revenue_usd metric.
    revenue_floor_usd: float = 0.0
    revenue_drop_pct: float = 60.0
    revenue_baseline_days: int = 7
    # H6/PERF-001/SEC-O-005 triage-storm cap: max borderline messages given an
    # LLM triage call in a single monitor cycle (see alerts/monitor.py).
    triage_max_per_cycle: int = 20


class InboundSourceCfg(BaseModel):
    secret_env: str


class CommsConfig(BaseModel):
    inbound_max_bytes: int = 262144
    rate_limit_per_minute: int = 60
    sources: dict[str, InboundSourceCfg] = Field(default_factory=dict)


class CurationConfig(BaseModel):
    extract_vip: bool = True
    extract_goal_keywords: bool = True
    batch_limit: int = 50


class AutonomyConfig(BaseModel):
    rung: int = 1


class VoiceConfig(BaseModel):
    default_provider: str = "elevenlabs"
    elevenlabs_voice_id: str | None = None
    xai_candidates: list[dict[str, Any]] = Field(default_factory=list)


class EdcAccountCfg(BaseModel):
    """One comms source → its owner and company (Executive Decision Center §8).

    Keyed in ``EdcConfig.accounts`` by the ``comms_messages.source`` name (the
    IMAP/broker mailbox source the poller writes). A source absent from the map
    has NO owner: EDC skips it loudly rather than guessing whose queue it is.
    """

    owner_employee_id: str
    company_slug: str = ""
    # The broker connector this mailbox sends through (e.g. 'gmail_ownera').
    # Denormalized onto every Decision so the reply/execute path knows which
    # owner-scoped connector to use without re-deriving it.
    source_account: str = ""


class EdcConfig(BaseModel):
    """Executive Decision Center tenancy + cadence (synthesis §8/§11)."""

    # source name -> owner/company/account. Static, git-reviewable (chosen over
    # stashing the owner in the mutable comms_sources.config_json blob).
    accounts: dict[str, EdcAccountCfg] = Field(default_factory=dict)
    # Max edc_classify LLM calls per triage cycle; overflow waits for the next
    # tick (mirrors alerts.triage_max_per_cycle).
    llm_max_per_cycle: int = 20
    # Unattended auto-delegation stays OFF until the operator trusts a promoted rule
    # (synthesis §9); matches arrive pre-filled for one-tap approval meanwhile.
    auto_delegate_live: bool = False
    # OPUS-MAJOR sender-credibility gate: a message may reach URGENT only if its
    # sender is DKIM/SPF-verified (Authentication-Results, aligned to From) OR its
    # From domain is on this small, git-reviewable allowlist of known billing/
    # provider senders. An unverified, non-allowlisted sender caps at NEEDS_OWNER —
    # scary wording from a stranger is exactly the fake-urgency the operator must not be
    # pinged about. Payment-failure notices from these billing providers must
    # STAY urgent; keep it short and extend by PR.
    credible_sender_domains: list[str] = Field(
        default_factory=lambda: [
            "freshworks.com",
            "freshdesk.com",
            "mailgun.net",
            "mailgun.com",
            "stripe.com",
            "google.com",
            "accounts.google.com",
            "paypal.com",
            "amazon.com",
            "aws.amazon.com",
            "github.com",
        ]
    )


class StewardConfig(BaseModel):
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    comms: CommsConfig = Field(default_factory=CommsConfig)
    curation: CurationConfig = Field(default_factory=CurationConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    edc: EdcConfig = Field(default_factory=EdcConfig)


def _default_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "steward.yaml"


def load_steward_config(path: str | None = None) -> StewardConfig:
    """Load Steward YAML, returning model defaults when the file is absent."""
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        return StewardConfig()
    raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    return StewardConfig.model_validate(raw or {})


__all__ = [
    "AlertsConfig",
    "AutonomyConfig",
    "BriefingConfig",
    "CommsConfig",
    "CurationConfig",
    "EdcAccountCfg",
    "EdcConfig",
    "InboundSourceCfg",
    "StewardConfig",
    "VoiceConfig",
    "load_steward_config",
]
