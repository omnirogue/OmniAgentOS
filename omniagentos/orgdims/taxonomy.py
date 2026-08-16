"""Controlled vocabularies for multidimensional organization (P1)."""

from __future__ import annotations

from typing import Any

TAXONOMY_VERSION = 1

# Authoritative company → product structure from the upgrade plan §16.
COMPANIES: list[dict[str, Any]] = [
    {
        "slug": "initech",
        "name": "Initech",
        "products": [
            {"slug": "initech-enterprise", "name": "Initech Enterprise"},
            {"slug": "initech-dtc", "name": "Initech Direct to Consumer"},
        ],
    },
    {
        "slug": "globex",
        "name": "Globex",
        "products": [
            {"slug": "globex-enterprise", "name": "Globex Enterprise"},
            {"slug": "globex-dtc", "name": "Globex Direct to Consumer"},
        ],
    },
    {
        "slug": "acmeuni",
        "name": "AcmeUni",
        "products": [
            {"slug": "acmeuni-dtc", "name": "AcmeUni Direct to Consumer"},
            {"slug": "acmeuni-claude-pro-university", "name": "AcmeUni Agent Pro Academy"},
        ],
    },
    # JG2-E13c — the roster is six companies. These three are owner folders (D3),
    # so each carries ONE self-titled product rather than an invented product
    # line: every company in this table must have at least one product
    # (tests/taxonomy/test_taxonomy_and_classification.py), and a self-titled
    # entry is the honest minimum until the operator names real ones.
    {
        "slug": "hooli",
        "name": "Hooli",
        "products": [
            {"slug": "hooli", "name": "Hooli"},
        ],
    },
    {
        "slug": "omniagentos",
        "name": "OmniAgentOS",
        "products": [
            {"slug": "omniagentos-platform", "name": "OmniAgentOS Platform"},
        ],
    },
    {
        "slug": "personal",
        "name": "Personal",
        "products": [
            {"slug": "personal", "name": "Personal"},
        ],
    },
]

WORKSTREAMS: list[dict[str, Any]] = [
    {
        "slug": "engineering",
        "label": "Engineering",
        "aliases": ["coding", "code", "dev", "devops", "infra", "ai systems"],
        "domains": [
            "backend",
            "frontend",
            "architecture",
            "qa",
            "security",
            "devops",
            "infrastructure",
            "ai-systems",
        ],
    },
    {
        "slug": "product",
        "label": "Product",
        "aliases": ["prototypes", "ux", "roadmap"],
        "domains": ["discovery", "requirements", "prototype", "ux", "roadmap", "analytics"],
    },
    {
        "slug": "creative",
        "label": "Creative",
        "aliases": ["creatives", "content", "brand"],
        "domains": [
            "copywriting",
            "email",
            "webinar",
            "image",
            "video",
            "audio",
            "voice",
            "landing-page",
            "brand",
        ],
    },
    {
        "slug": "advertising",
        "label": "Advertising",
        "aliases": ["ads", "media buying", "meta ads", "paid"],
        "domains": ["meta-ads", "google-ads", "campaigns", "attribution", "creative-testing"],
    },
    {
        "slug": "marketing",
        "label": "Marketing",
        "aliases": ["growth", "lifecycle", "launch"],
        "domains": ["lifecycle", "organic", "affiliates", "launches", "positioning"],
    },
    {
        "slug": "sales",
        "label": "Sales",
        "aliases": ["enterprise sales", "outbound", "crm"],
        "domains": ["enterprise", "dtc-conversion", "outbound", "partnerships", "proposals"],
    },
    {
        "slug": "customer-success",
        "label": "Customer Success & Support",
        "aliases": ["support", "cs", "onboarding", "retention"],
        "domains": ["onboarding", "support", "retention", "education", "community"],
    },
    {
        "slug": "operations",
        "label": "Operations",
        "aliases": ["ops", "sop", "automation"],
        "domains": ["sops", "staffing", "vendors", "coordination", "business-automation"],
    },
    {
        "slug": "finance-admin",
        "label": "Finance/Admin",
        "aliases": ["finance", "admin", "billing", "bookkeeping"],
        "domains": [
            "bookkeeping",
            "forecasting",
            "billing",
            "reconciliation",
            "procurement",
            "admin",
        ],
    },
    {
        "slug": "legal-compliance",
        "label": "Legal/Compliance",
        "aliases": ["legal", "compliance", "privacy", "contracts"],
        "domains": ["contracts", "privacy", "platform-policies", "claims-review", "audit"],
    },
    {
        "slug": "research",
        "label": "Research & Intelligence",
        "aliases": ["research", "intelligence", "competitive"],
        "domains": ["market", "competitor", "model", "technical", "customer-research"],
    },
    {
        "slug": "data-analytics",
        "label": "Data & Analytics",
        "aliases": ["data", "analytics", "metrics", "bi"],
        "domains": ["metrics", "experimentation", "reporting", "warehouse", "decision-systems"],
    },
    {
        "slug": "executive-strategy",
        "label": "Executive/Strategy",
        "aliases": ["executive", "strategy", "portfolio"],
        "domains": ["portfolio", "capital", "strategy", "company-goals"],
    },
]

CHANNELS = [
    "meta",
    "google",
    "email",
    "webinar",
    "dashboard",
    "api",
    "stripe",
    "support-inbox",
    "slack",
    "telegram",
]

LIFECYCLES = [
    "backlog",
    "ready",
    "running",
    "verifying",
    "blocked",
    "released",
    "observing",
    "complete",
]

# Map plan risk vocabulary onto Omni action-class spirit.
RISK_CLASSES = [
    "read_only",
    "reversible_internal",
    "bounded_external",
    "irreversible",
]

PRIORITIES = ["low", "normal", "high", "critical", "urgent"]

# Legacy category → workstream migration map (§15).
LEGACY_CATEGORY_MAP: dict[str, str] = {
    "coding": "engineering",
    "code": "engineering",
    "prototypes": "product",
    "prototype": "product",
    "creatives": "creative",
    "creative": "creative",
    "advertising": "advertising",
    "ads": "advertising",
}


def all_workstream_slugs() -> set[str]:
    return {w["slug"] for w in WORKSTREAMS}


def all_domain_slugs() -> set[str]:
    out: set[str] = set()
    for w in WORKSTREAMS:
        out.update(w.get("domains") or [])
    return out


def resolve_workstream_alias(raw: str) -> str | None:
    text = raw.strip().lower().replace("_", "-").replace(" ", "-")
    if text in all_workstream_slugs():
        return text
    for w in WORKSTREAMS:
        aliases = [a.lower().replace(" ", "-") for a in w.get("aliases") or []]
        if text in aliases or raw.strip().lower() in [a.lower() for a in w.get("aliases") or []]:
            return str(w["slug"])
        if text == str(w["label"]).lower().replace(" ", "-"):
            return str(w["slug"])
    return LEGACY_CATEGORY_MAP.get(text) or LEGACY_CATEGORY_MAP.get(raw.strip().lower())
