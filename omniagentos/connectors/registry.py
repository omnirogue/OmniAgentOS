"""Integration catalog — the owner's real stack, surfaced as a connections panel.

Two concerns live here and nowhere else:

1. The declarative catalog of known integrations (id, name, category, logo,
   docs_url, env-key families). This is the list of systems the operator has
   ever plausibly wired in. Pure data; never does I/O except to read the vault.

2. The vault parser. It reads ``~/.config/omni/connections.env`` and extracts
   ONLY the KEY NAMES (``^[A-Z0-9_]+(?==)``). Values are never read into memory,
   never returned, never logged. If the vault is unreadable every integration
   reports ``status="error"`` gracefully — the page still renders.

Status semantics (per-integration):

  * ``connected``      — every declared key family is present in the vault
  * ``configured``     — at least one key family present, but not all
  * ``not_configured`` — no declared keys present
  * ``error``          — vault was unreadable at read time

Multi-instance integrations (PiedPiper, Meta Ads, PayPal) model sub-instances
as a list of ``(label, key-family-prefix)`` tuples. Each instance is reported
independently; the integration's top-level status is the most-favorable rollup
(connected if ALL instances are connected; configured if ANY are; not_configured
if NONE are).

Connector-store state (``omniagentos/connectors/store.py``) is layered on
read-only — if any agent holds a grant for a connector's capabilities, the
detail line includes "N agents granted". This is a UI nicety, not a source of
truth for status.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ── Category ordering (the operator's preferred scan order) ────────────────

CATEGORY_ORDER: tuple[tuple[str, str], ...] = (
    ("ai_providers", "AI Providers"),
    ("crm_marketing", "CRM & Marketing"),
    ("email_comms", "Email & Comms"),
    ("advertising", "Advertising"),
    ("payments", "Payments"),
    ("banking_finance", "Banking & Finance"),
    ("media_voice", "Media & Voice"),
    ("infrastructure", "Infrastructure & Cloud"),
    ("analytics", "Analytics & Monitoring"),
)


# ── Catalog models ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Instance:
    """A named sub-instance of a multi-instance integration.

    ``key_prefix`` is a FAMILY label like ``ACMEUNI_PIEDPIPER_`` — any vault key beginning
    with this prefix belongs to this instance. The prefix itself is not a secret,
    it's a naming convention the operator uses to separate sub-accounts.
    """

    label: str
    key_prefix: str


@dataclass(frozen=True)
class Integration:
    """One external system the operator has ever wired in."""

    id: str
    name: str
    category: str
    logo: str  # matches brandIcons.tsx component name
    docs_url: str | None
    # For single-instance integrations: the list of env-key names whose presence
    # means "configured". For multi-instance: use ``instances`` instead.
    key_families: tuple[str, ...] = ()
    # Multi-instance integrations: each instance contributes its own status chip.
    instances: tuple[Instance, ...] = ()
    # Human one-liner describing what the integration unlocks in the OS. Shown in
    # the detail Dialog. Not a secret; just a sentence.
    unlocks: str = ""

    @property
    def is_multi_instance(self) -> bool:
        return bool(self.instances)


# ── The catalog (hand-maintained by the operator; order within a category is preserved) ──


def build_catalog() -> list[Integration]:
    """Return the full ordered catalog. Pure — no I/O."""
    items: list[Integration] = []

    # ── AI Providers ─────────────────────────────────────────────────────
    items.append(
        Integration(
            id="anthropic",
            name="Anthropic",
            category="ai_providers",
            logo="anthropic",
            docs_url="https://docs.anthropic.com",
            key_families=("ANTHROPIC_API_KEY",),
            unlocks="Claude models — frontier reasoning, long-context planning, code generation.",
        )
    )
    items.append(
        Integration(
            id="openai",
            name="OpenAI",
            category="ai_providers",
            logo="openai",
            docs_url="https://platform.openai.com/docs",
            key_families=("OPENAI_API_KEY",),
            unlocks="GPT models — general-purpose completions, embeddings, vision.",
        )
    )
    items.append(
        Integration(
            id="xai",
            name="xAI (Grok)",
            category="ai_providers",
            logo="xai",
            docs_url="https://docs.x.ai",
            key_families=("XAI_API_KEY",),
            unlocks="Grok models — real-time knowledge, conversational agents.",
        )
    )
    items.append(
        Integration(
            id="google_ai",
            name="Google AI / Gemini",
            category="ai_providers",
            logo="gemini",
            docs_url="https://ai.google.dev",
            key_families=("GOOGLE_AI_API_KEY", "GEMINI_API_KEY"),
            unlocks="Gemini models — multimodal reasoning, large context windows.",
        )
    )
    items.append(
        Integration(
            id="moonshot",
            name="Moonshot (Kimi)",
            category="ai_providers",
            logo="kimi",
            docs_url="https://platform.moonshot.cn/docs",
            key_families=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
            unlocks="Kimi models — deep research, long-document analysis.",
        )
    )
    items.append(
        Integration(
            id="fireworks",
            name="Fireworks AI",
            category="ai_providers",
            logo="fireworks",
            docs_url="https://docs.fireworks.ai",
            key_families=("FIREWORKS_API_KEY",),
            unlocks="Hosted open models — the default paid Kimi K3 route.",
        )
    )
    items.append(
        Integration(
            id="openrouter",
            name="OpenRouter",
            category="ai_providers",
            logo="openrouter",
            docs_url="https://openrouter.ai/docs",
            key_families=("OPENROUTER_API_KEY",),
            unlocks="Aggregated model access — cascade fallback, model arbitrage.",
        )
    )
    items.append(
        Integration(
            id="together",
            name="Together",
            category="ai_providers",
            logo="together",
            docs_url="https://docs.together.ai",
            key_families=("TOGETHER_API_KEY",),
            unlocks="Open-source model hosting — Llama, Mixtral, embedding models.",
        )
    )
    items.append(
        Integration(
            id="mistral",
            name="Mistral",
            category="ai_providers",
            logo="mistral",
            docs_url="https://docs.mistral.ai",
            key_families=("MISTRAL_API_KEY",),
            unlocks="Mistral models — efficient European-hosted inference.",
        )
    )
    items.append(
        Integration(
            id="replicate",
            name="Replicate",
            category="ai_providers",
            logo="replicate",
            docs_url="https://replicate.com/docs",
            key_families=("REPLICATE_API_KEY",),
            unlocks="Image, video, and audio generation models on demand.",
        )
    )

    # ── CRM & Marketing ──────────────────────────────────────────────────
    items.append(
        Integration(
            id="piedpiper",
            name="PiedPiper",
            category="crm_marketing",
            logo="piedpiper",
            docs_url="https://highlevel.stoplight.io/docs",
            instances=(
                Instance(label="AcmeUni", key_prefix="PIEDPIPER_ACMEUNI_"),
                Instance(label="INITECH", key_prefix="PIEDPIPER_INITECH_"),
                Instance(label="GLOBEX", key_prefix="PIEDPIPER_GLOBEX_"),
            ),
            unlocks="CRM, pipelines, automation, messaging — one per brand sub-account.",
        )
    )

    # ── Email & Comms ────────────────────────────────────────────────────
    items.append(
        Integration(
            id="gmail",
            name="Gmail / Google",
            category="email_comms",
            logo="gmail",
            docs_url="https://developers.google.com/gmail/api",
            key_families=(
                "GOOGLE_OAUTH_CLIENT_ID",
                "GOOGLE_OAUTH_CLIENT_SECRET",
                "GOOGLE_OAUTH_REFRESH_TOKEN",
            ),
            unlocks="Inbox read/write — draft, send, triage mail on the operator's behalf.",
        )
    )
    items.append(
        Integration(
            id="slack",
            name="Slack",
            category="email_comms",
            logo="slack",
            docs_url="https://api.slack.com",
            key_families=("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"),
            unlocks="Workspace messaging — post updates, respond in threads, notify channels.",
        )
    )
    items.append(
        Integration(
            id="telegram",
            name="Telegram",
            category="email_comms",
            logo="telegram",
            docs_url="https://core.telegram.org/bots/api",
            key_families=("TELEGRAM_BOT_TOKEN",),
            unlocks="Bot messaging — direct channel alerts and two-way chat with the operator.",
        )
    )

    # ── Advertising ──────────────────────────────────────────────────────
    items.append(
        Integration(
            id="meta_ads",
            name="Meta Ads",
            category="advertising",
            logo="meta",
            docs_url="https://developers.facebook.com/docs/marketing-api",
            instances=(
                Instance(label="AcmeUni", key_prefix="ACMEUNI_META_"),
                Instance(label="INITECH", key_prefix="INITECH_META_"),
                Instance(label="GLOBEX", key_prefix="GLOBEX_META_"),
            ),
            unlocks="Ad account management — creatives, campaign budgets, performance reads.",
        )
    )

    # ── Payments ─────────────────────────────────────────────────────────
    items.append(
        Integration(
            id="stripe",
            name="Stripe",
            category="payments",
            logo="stripe",
            docs_url="https://docs.stripe.com",
            key_families=("STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY"),
            unlocks="Card payments, subscriptions, invoices, revenue reporting.",
        )
    )
    items.append(
        Integration(
            id="paypal",
            name="PayPal",
            category="payments",
            logo="paypal",
            docs_url="https://developer.paypal.com/api/rest",
            instances=(
                Instance(label="Primary", key_prefix="PAYPAL_"),
                Instance(label="Secondary", key_prefix="PAYPAL_ALT_"),
            ),
            unlocks="PayPal payments and payouts across multiple merchant accounts.",
        )
    )
    items.append(
        Integration(
            id="vandelay",
            name="Vandelay",
            category="payments",
            logo="plug",
            docs_url="https://vandelay.example/api",
            key_families=("VANDELAY_API_KEY",),
            unlocks="High-risk merchant processing and recurring billing.",
        )
    )
    items.append(
        Integration(
            id="fanbasis",
            name="Fanbasis",
            category="payments",
            logo="plug",
            docs_url=None,
            key_families=("FANBASIS_API_KEY",),
            unlocks="Creator-economy payments and fan subscriptions.",
        )
    )
    items.append(
        Integration(
            id="nmi",
            name="NMI",
            category="payments",
            logo="plug",
            docs_url="https://secure.networkmerchants.com/gw/merchants/resources/integration/integration_portal.php",
            key_families=("NMI_API_KEY",),
            unlocks="Multi-gateway payment routing for high-risk and APM flows.",
        )
    )

    # ── Banking & Finance ────────────────────────────────────────────────
    items.append(
        Integration(
            id="slash",
            name="Slash",
            category="banking_finance",
            logo="plug",
            docs_url=None,
            key_families=("SLASH_API_KEY",),
            unlocks="Business banking and cash-flow reads.",
        )
    )
    items.append(
        Integration(
            id="teller",
            name="Teller",
            category="banking_finance",
            logo="teller",
            docs_url="https://teller.io/docs",
            key_families=("TELLER_APP_ID", "TELLER_SIGNING_SECRET"),
            unlocks="Bank-account aggregation — balances, transactions, real-time feeds.",
        )
    )
    items.append(
        Integration(
            id="chargeblast",
            name="Chargeblast",
            category="banking_finance",
            logo="plug",
            docs_url="https://www.chargeblast.com",
            key_families=("CHARGEBLAST_API_KEY",),
            unlocks="Chargeback recovery — dispute automation and recovery analytics.",
        )
    )

    # ── Media & Voice ────────────────────────────────────────────────────
    items.append(
        Integration(
            id="elevenlabs",
            name="ElevenLabs",
            category="media_voice",
            logo="elevenlabs",
            docs_url="https://elevenlabs.io/docs",
            key_families=("ELEVENLABS_API_KEY",),
            unlocks="Voice synthesis — narrations, podcasts, voice-enabled agents.",
        )
    )
    items.append(
        Integration(
            id="pexels",
            name="Pexels",
            category="media_voice",
            logo="pexels",
            docs_url="https://www.pexels.com/api",
            key_families=("PEXELS_API_KEY",),
            unlocks="Royalty-free stock photography and video for outbound content.",
        )
    )
    items.append(
        Integration(
            id="pixabay",
            name="Pixabay",
            category="media_voice",
            logo="pixabay",
            docs_url="https://pixabay.com/api/docs",
            key_families=("PIXABAY_API_KEY",),
            unlocks="Royalty-free images and vectors for social and blog content.",
        )
    )
    items.append(
        Integration(
            id="jamendo",
            name="Jamendo",
            category="media_voice",
            logo="plug",
            docs_url="https://devportal.jamendo.com",
            key_families=("JAMENDO_CLIENT_ID",),
            unlocks="Royalty-free music licensing for video and audio content.",
        )
    )
    items.append(
        Integration(
            id="sync",
            name="Sync",
            category="media_voice",
            logo="plug",
            docs_url=None,
            key_families=("SYNC_API_KEY",),
            unlocks="Sync licensing for commercial music placements.",
        )
    )

    # ── Infrastructure & Cloud ───────────────────────────────────────────
    items.append(
        Integration(
            id="cloudflare",
            name="Cloudflare",
            category="infrastructure",
            logo="cloudflare",
            docs_url="https://developers.cloudflare.com/api",
            key_families=("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
            unlocks="DNS, CDN, Workers — edge compute and traffic routing.",
        )
    )
    items.append(
        Integration(
            id="aws",
            name="AWS + S3",
            category="infrastructure",
            logo="aws",
            docs_url="https://docs.aws.amazon.com",
            key_families=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
            unlocks="Object storage, compute, and managed services.",
        )
    )
    items.append(
        Integration(
            id="runpod",
            name="RunPod",
            category="infrastructure",
            logo="runpod",
            docs_url="https://docs.runpod.io",
            key_families=("RUNPOD_API_KEY",),
            unlocks="GPU pods for fine-tuning and on-demand inference.",
        )
    )
    items.append(
        Integration(
            id="huggingface",
            name="HuggingFace",
            category="infrastructure",
            logo="huggingface",
            docs_url="https://huggingface.co/docs/hub",
            key_families=("HUGGINGFACE_TOKEN",),
            unlocks="Model hub, datasets, and Inference Endpoints.",
        )
    )

    # ── Analytics & Monitoring ───────────────────────────────────────────
    items.append(
        Integration(
            id="checkly",
            name="Checkly",
            category="analytics",
            logo="plug",
            docs_url="https://www.checklyhq.com/docs/api",
            key_families=("CHECKLY_API_KEY", "CHECKLY_ACCOUNT_ID"),
            unlocks="Synthetic monitoring — uptime, API checks, browser tests.",
        )
    )
    items.append(
        Integration(
            id="livesession",
            name="LiveSession",
            category="analytics",
            logo="plug",
            docs_url="https://help.livesession.io",
            key_families=("LIVESESSION_TRACKING_ID",),
            unlocks="Session replays and product analytics.",
        )
    )
    items.append(
        Integration(
            id="vwo",
            name="VWO",
            category="analytics",
            logo="plug",
            docs_url="https://developers.vwo.com",
            key_families=("VWO_ACCOUNT_ID", "VWO_API_KEY"),
            unlocks="A/B testing and conversion optimization.",
        )
    )
    items.append(
        Integration(
            id="kowboykit",
            name="Kowboykit",
            category="analytics",
            logo="plug",
            docs_url=None,
            key_families=("KOWBOYKIT_API_KEY",),
            unlocks="Business analytics and funnel tracking.",
        )
    )

    return items


CATALOG: tuple[Integration, ...] = tuple(build_catalog())


# ── Vault parser ───────────────────────────────────────────────────────────


#: The ONE grammar for a vault assignment line, shared by every reader of the
#: vault so they cannot drift apart again. It matches an optional ``export``
#: prefix and leading indentation, then a POSIX-shaped variable name, and
#: captures the NAME only -- the value is never in a capture group, so no reader
#: can accidentally retain one.
#:
#: This used to be ``^[A-Z0-9_]+(?==)`` here and
#: ``^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=`` in ``inventory.py``, for the same
#: file. A vault written in shell-sourceable ``export NAME=`` form was therefore
#: fully visible to the inventory and entirely invisible to the dashboard, which
#: rendered live connectors as ``not_configured``.
_VAULT_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE
)


def vault_key_names(text: str) -> frozenset[str]:
    """Extract the variable NAMES assigned in vault text. Never returns a value.

    The single implementation of the vault grammar. ``parse_vault`` (the
    dashboard's reader) and ``connectors.inventory._parse_env_file`` (the
    secrets-inventory reader) both go through here, so "what is a vault
    assignment" has exactly one answer for the file they share.
    """
    return frozenset(_VAULT_ASSIGNMENT_RE.findall(text))


def default_vault_path() -> Path:
    """The canonical location of the operator's connections vault."""
    override = os.environ.get("OMNIAGENTOS_CONNECTIONS_VAULT")
    if override:
        return Path(override)
    return Path.home() / ".config" / "omni" / "connections.env"


def parse_vault(path: Path) -> tuple[frozenset[str], bool]:
    """Read the vault and return ``(key_names, readable)``.

    Extracts ONLY key names, through the shared :func:`vault_key_names` grammar.
    Values are not captured into memory. If the file cannot be read or parsed,
    returns ``(frozenset(), False)`` — callers should surface ``status="error"``
    rather than crash.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return frozenset(), False
    try:
        names = vault_key_names(text)
    except re.error:
        return frozenset(), False
    return names, True


# ── Status rollup ──────────────────────────────────────────────────────────


Status = str  # "connected" | "configured" | "not_configured" | "error"


@dataclass(frozen=True)
class InstanceStatus:
    label: str
    status: Status
    # Which key prefixes resolved (family label, not env-var names), for the
    # masked checklist the detail Dialog shows.
    matched_prefix: str | None = None


@dataclass(frozen=True)
class IntegrationView:
    id: str
    name: str
    logo: str
    status: Status
    instances: tuple[InstanceStatus, ...]
    detail: str  # human summary, e.g. "3 keys configured"
    docs_url: str | None
    unlocks: str
    # How many agents currently hold grants against this connector's capabilities.
    agents_granted: int = 0


@dataclass(frozen=True)
class CategoryView:
    id: str
    label: str
    integrations: tuple[IntegrationView, ...]


def _rollup_one(
    key_families: Iterable[str],
    vault_keys: frozenset[str],
) -> tuple[Status, int]:
    """For a single-instance integration: status + count of present families."""
    families = tuple(key_families)
    present = [f for f in families if f in vault_keys]
    if not families:
        return "not_configured", 0
    if len(present) == len(families):
        return "connected", len(present)
    if present:
        return "configured", len(present)
    return "not_configured", 0


def _rollup_instance(
    instance: Instance,
    vault_keys: frozenset[str],
) -> InstanceStatus:
    """For one sub-instance of a multi-instance integration."""
    any_match = any(k.startswith(instance.key_prefix) for k in vault_keys)
    if any_match:
        return InstanceStatus(
            label=instance.label,
            status="connected",
            matched_prefix=instance.key_prefix,
        )
    return InstanceStatus(label=instance.label, status="not_configured")


def _combine_instance_statuses(statuses: Iterable[Status]) -> Status:
    """Pick the most-favorable rollup across instances."""
    as_list = list(statuses)
    if not as_list:
        return "not_configured"
    if all(s == "connected" for s in as_list):
        return "connected"
    if any(s in ("connected", "configured") for s in as_list):
        return "configured"
    return "not_configured"


# ── Connector-store overlay ────────────────────────────────────────────────


def _connector_id_for(integration_id: str) -> str:
    """Map an IntegrationView id to the legacy connector-registry id, if any.

    The legacy ``configs/connectors.yaml`` registry uses ids like ``piedpiper``,
    ``stripe``, ``slack``, ``gmail``. Anything outside that registry maps to
    ``""`` and yields zero grants.
    """
    # We look up by name match against the loaded connector registry — but only
    # opportunistically: if the registry is missing or malformed, return empty.
    try:
        from omniagentos.connectors import ConnectorError, load_registry

        registry = load_registry()
    except (ConnectorError, Exception):
        return ""
    # Prefer exact id match to the legacy registry.
    if integration_id in registry.connectors:
        return integration_id
    # Special-case: gmail integration ↔ ``gmail`` connector is exact; the rest
    # are named the same by convention.
    return ""


def _grants_for_connector(connector_id: str) -> int:
    """Count distinct agents holding at least one grant for a connector.

    Returns 0 when the capability store is unavailable or the connector has no
    capability rows — never raises.
    """
    if not connector_id:
        return 0
    try:
        from omniagentos.api.routes.access import get_capability_store

        cap_store = get_capability_store()
    except Exception:
        return 0
    try:
        all_grants = cap_store.grants_for_all()
    except Exception:
        return 0
    count = 0
    for _agent_id, caps in all_grants.items():
        if any(c.startswith(f"{connector_id}.") for c in caps):
            count += 1
    return count


# ── Public builder ─────────────────────────────────────────────────────────


def build_connections_view(
    vault_path: Path | None = None,
) -> tuple[list[CategoryView], bool]:
    """Return ``(categories, vault_readable)`` ready for the API response.

    The order of categories matches ``CATEGORY_ORDER``. Integrations within a
    category retain catalog order.
    """
    path = vault_path if vault_path is not None else default_vault_path()
    vault_keys, vault_readable = parse_vault(path)

    groups: dict[str, list[Integration]] = {cid: [] for cid, _ in CATEGORY_ORDER}
    for item in CATALOG:
        if item.category in groups:
            groups[item.category].append(item)

    categories: list[CategoryView] = []
    for cat_id, cat_label in CATEGORY_ORDER:
        views: list[IntegrationView] = []
        for item in groups.get(cat_id, []):
            if not vault_readable:
                view = IntegrationView(
                    id=item.id,
                    name=item.name,
                    logo=item.logo,
                    status="error",
                    instances=(),
                    detail="Vault unreadable",
                    docs_url=item.docs_url,
                    unlocks=item.unlocks,
                    agents_granted=0,
                )
            elif item.is_multi_instance:
                inst_statuses = tuple(
                    _rollup_instance(inst, vault_keys) for inst in item.instances
                )
                overall = _combine_instance_statuses(s.status for s in inst_statuses)
                connected_count = sum(
                    1 for s in inst_statuses if s.status == "connected"
                )
                detail = (
                    f"{connected_count}/{len(inst_statuses)} instances connected"
                )
                view = IntegrationView(
                    id=item.id,
                    name=item.name,
                    logo=item.logo,
                    status=overall,
                    instances=inst_statuses,
                    detail=detail,
                    docs_url=item.docs_url,
                    unlocks=item.unlocks,
                    agents_granted=0,
                )
            else:
                status, count = _rollup_one(item.key_families, vault_keys)
                detail = (
                    f"{count} key{'s' if count != 1 else ''} configured"
                    if status in ("connected", "configured")
                    else "Not configured"
                )
                view = IntegrationView(
                    id=item.id,
                    name=item.name,
                    logo=item.logo,
                    status=status,
                    instances=(),
                    detail=detail,
                    docs_url=item.docs_url,
                    unlocks=item.unlocks,
                    agents_granted=0,
                )

            # Overlay legacy connector-store grant count (best-effort).
            cid = _connector_id_for(view.id)
            if cid:
                try:
                    view = IntegrationView(
                        id=view.id,
                        name=view.name,
                        logo=view.logo,
                        status=view.status,
                        instances=view.instances,
                        detail=view.detail,
                        docs_url=view.docs_url,
                        unlocks=view.unlocks,
                        agents_granted=_grants_for_connector(cid),
                    )
                except Exception:
                    pass

            views.append(view)

        categories.append(
            CategoryView(
                id=cat_id,
                label=cat_label,
                integrations=tuple(views),
            )
        )

    return categories, vault_readable


def summary(categories: Iterable[CategoryView]) -> tuple[int, int]:
    """Return ``(connected_count, total_count)`` across all categories."""
    total = 0
    connected = 0
    for cat in categories:
        for view in cat.integrations:
            total += 1
            if view.status == "connected":
                connected += 1
    return connected, total


__all__ = [
    "CATEGORY_ORDER",
    "CATALOG",
    "CategoryView",
    "Instance",
    "InstanceStatus",
    "Integration",
    "IntegrationView",
    "Status",
    "build_catalog",
    "build_connections_view",
    "default_vault_path",
    "parse_vault",
    "summary",
]
