/**
 * Connections fixture — full catalog rendered as "all-connected" for dev/demo.
 *
 * Activated via NEXT_PUBLIC_USE_CONNECTIONS_FIXTURES=true. Lets the Connections
 * page render the complete panel during frontend iteration without a running
 * API (or without a vault file on disk).
 */

import type {
  ConnectionCategory,
  ConnectionIntegration,
  ConnectionsResponse,
  ConnectionStatus,
} from "./types";

interface FixtureEntry {
  id: string;
  name: string;
  logo: string;
  docs_url: string | null;
  unlocks: string;
  /** Optional: multi-instance fixture. */
  instances?: { label: string; status: ConnectionStatus }[];
  /** Optional override for single-instance; defaults to connected. */
  status?: ConnectionStatus;
}

const FIXTURE_CATALOG: { id: string; label: string; items: FixtureEntry[] }[] = [
  {
    id: "ai_providers",
    label: "AI Providers",
    items: [
      {
        id: "anthropic",
        name: "Anthropic",
        logo: "anthropic",
        docs_url: "https://docs.anthropic.com",
        unlocks:
          "Claude models — frontier reasoning, long-context planning, code generation.",
      },
      {
        id: "openai",
        name: "OpenAI",
        logo: "openai",
        docs_url: "https://platform.openai.com/docs",
        unlocks: "GPT models — general-purpose completions, embeddings, vision.",
      },
      {
        id: "xai",
        name: "xAI (Grok)",
        logo: "xai",
        docs_url: "https://docs.x.ai",
        unlocks: "Grok models — real-time knowledge, conversational agents.",
      },
      {
        id: "google_ai",
        name: "Google AI / Gemini",
        logo: "gemini",
        docs_url: "https://ai.google.dev",
        unlocks: "Gemini models — multimodal reasoning, large context windows.",
      },
      {
        id: "moonshot",
        name: "Moonshot (Kimi)",
        logo: "kimi",
        docs_url: "https://platform.moonshot.cn/docs",
        unlocks: "Kimi models — deep research, long-document analysis.",
      },
      {
        id: "openrouter",
        name: "OpenRouter",
        logo: "openrouter",
        docs_url: "https://openrouter.ai/docs",
        unlocks: "Aggregated model access — cascade fallback, model arbitrage.",
      },
      {
        id: "together",
        name: "Together",
        logo: "together",
        docs_url: "https://docs.together.ai",
        unlocks: "Open-source model hosting — Llama, Mixtral, embedding models.",
      },
      {
        id: "mistral",
        name: "Mistral",
        logo: "mistral",
        docs_url: "https://docs.mistral.ai",
        unlocks: "Mistral models — efficient European-hosted inference.",
      },
      {
        id: "replicate",
        name: "Replicate",
        logo: "replicate",
        docs_url: "https://replicate.com/docs",
        unlocks: "Image, video, and audio generation models on demand.",
      },
    ],
  },
  {
    id: "crm_marketing",
    label: "CRM & Marketing",
    items: [
      {
        id: "piedpiper",
        name: "PiedPiper",
        logo: "piedpiper",
        docs_url: "https://highlevel.stoplight.io/docs",
        unlocks:
          "CRM, pipelines, automation, messaging — one per brand sub-account.",
        instances: [
          { label: "AcmeUni", status: "connected" },
          { label: "INITECH", status: "connected" },
          { label: "GLOBEX", status: "connected" },
        ],
      },
    ],
  },
  {
    id: "email_comms",
    label: "Email & Comms",
    items: [
      {
        id: "gmail",
        name: "Gmail / Google",
        logo: "gmail",
        docs_url: "https://developers.google.com/gmail/api",
        unlocks:
          "Inbox read/write — draft, send, triage mail on the operator's behalf.",
      },
      {
        id: "slack",
        name: "Slack",
        logo: "slack",
        docs_url: "https://api.slack.com",
        unlocks:
          "Workspace messaging — post updates, respond in threads, notify channels.",
      },
      {
        id: "telegram",
        name: "Telegram",
        logo: "telegram",
        docs_url: "https://core.telegram.org/bots/api",
        unlocks:
          "Bot messaging — direct channel alerts and two-way chat with the operator.",
      },
    ],
  },
  {
    id: "advertising",
    label: "Advertising",
    items: [
      {
        id: "meta_ads",
        name: "Meta Ads",
        logo: "meta",
        docs_url: "https://developers.facebook.com/docs/marketing-api",
        unlocks:
          "Ad account management — creatives, campaign budgets, performance reads.",
        instances: [
          { label: "AcmeUni", status: "connected" },
          { label: "INITECH", status: "connected" },
          { label: "GLOBEX", status: "connected" },
        ],
      },
    ],
  },
  {
    id: "payments",
    label: "Payments",
    items: [
      {
        id: "stripe",
        name: "Stripe",
        logo: "stripe",
        docs_url: "https://docs.stripe.com",
        unlocks: "Card payments, subscriptions, invoices, revenue reporting.",
      },
      {
        id: "paypal",
        name: "PayPal",
        logo: "paypal",
        docs_url: "https://developer.paypal.com/api/rest",
        unlocks:
          "PayPal payments and payouts across multiple merchant accounts.",
        instances: [
          { label: "Primary", status: "connected" },
          { label: "Secondary", status: "connected" },
        ],
      },
      {
        id: "vandelay",
        name: "Vandelay",
        logo: "plug",
        docs_url: "https://vandelay.example/api",
        unlocks: "High-risk merchant processing and recurring billing.",
      },
      {
        id: "fanbasis",
        name: "Fanbasis",
        logo: "plug",
        docs_url: null,
        unlocks: "Creator-economy payments and fan subscriptions.",
      },
      {
        id: "nmi",
        name: "NMI",
        logo: "plug",
        docs_url:
          "https://secure.networkmerchants.com/gw/merchants/resources/integration/integration_portal.php",
        unlocks:
          "Multi-gateway payment routing for high-risk and APM flows.",
      },
    ],
  },
  {
    id: "banking_finance",
    label: "Banking & Finance",
    items: [
      {
        id: "slash",
        name: "Slash",
        logo: "plug",
        docs_url: null,
        unlocks: "Business banking and cash-flow reads.",
      },
      {
        id: "teller",
        name: "Teller",
        logo: "teller",
        docs_url: "https://teller.io/docs",
        unlocks:
          "Bank-account aggregation — balances, transactions, real-time feeds.",
      },
      {
        id: "chargeblast",
        name: "Chargeblast",
        logo: "plug",
        docs_url: "https://www.chargeblast.com",
        unlocks: "Chargeback recovery — dispute automation and recovery analytics.",
      },
    ],
  },
  {
    id: "media_voice",
    label: "Media & Voice",
    items: [
      {
        id: "elevenlabs",
        name: "ElevenLabs",
        logo: "elevenlabs",
        docs_url: "https://elevenlabs.io/docs",
        unlocks: "Voice synthesis — narrations, podcasts, voice-enabled agents.",
      },
      {
        id: "pexels",
        name: "Pexels",
        logo: "pexels",
        docs_url: "https://www.pexels.com/api",
        unlocks: "Royalty-free stock photography and video for outbound content.",
      },
      {
        id: "pixabay",
        name: "Pixabay",
        logo: "pixabay",
        docs_url: "https://pixabay.com/api/docs",
        unlocks: "Royalty-free images and vectors for social and blog content.",
      },
      {
        id: "jamendo",
        name: "Jamendo",
        logo: "plug",
        docs_url: "https://devportal.jamendo.com",
        unlocks: "Royalty-free music licensing for video and audio content.",
      },
      {
        id: "sync",
        name: "Sync",
        logo: "plug",
        docs_url: null,
        unlocks: "Sync licensing for commercial music placements.",
      },
    ],
  },
  {
    id: "infrastructure",
    label: "Infrastructure & Cloud",
    items: [
      {
        id: "cloudflare",
        name: "Cloudflare",
        logo: "cloudflare",
        docs_url: "https://developers.cloudflare.com/api",
        unlocks: "DNS, CDN, Workers — edge compute and traffic routing.",
      },
      {
        id: "aws",
        name: "AWS + S3",
        logo: "aws",
        docs_url: "https://docs.aws.amazon.com",
        unlocks: "Object storage, compute, and managed services.",
      },
      {
        id: "runpod",
        name: "RunPod",
        logo: "runpod",
        docs_url: "https://docs.runpod.io",
        unlocks: "GPU pods for fine-tuning and on-demand inference.",
      },
      {
        id: "huggingface",
        name: "HuggingFace",
        logo: "huggingface",
        docs_url: "https://huggingface.co/docs/hub",
        unlocks: "Model hub, datasets, and Inference Endpoints.",
      },
    ],
  },
  {
    id: "analytics",
    label: "Analytics & Monitoring",
    items: [
      {
        id: "checkly",
        name: "Checkly",
        logo: "plug",
        docs_url: "https://www.checklyhq.com/docs/api",
        unlocks: "Synthetic monitoring — uptime, API checks, browser tests.",
      },
      {
        id: "livesession",
        name: "LiveSession",
        logo: "plug",
        docs_url: "https://help.livesession.io",
        unlocks: "Session replays and product analytics.",
      },
      {
        id: "vwo",
        name: "VWO",
        logo: "plug",
        docs_url: "https://developers.vwo.com",
        unlocks: "A/B testing and conversion optimization.",
      },
      {
        id: "kowboykit",
        name: "Kowboykit",
        logo: "plug",
        docs_url: null,
        unlocks: "Business analytics and funnel tracking.",
      },
    ],
  },
];

function toIntegration(entry: FixtureEntry): ConnectionIntegration {
  if (entry.instances) {
    return {
      id: entry.id,
      name: entry.name,
      logo: entry.logo,
      status: entry.status ?? "connected",
      instances: entry.instances,
      detail: `${entry.instances.filter((i) => i.status === "connected").length}/${entry.instances.length} instances connected`,
      docs_url: entry.docs_url,
      unlocks: entry.unlocks,
    };
  }
  return {
    id: entry.id,
    name: entry.name,
    logo: entry.logo,
    status: entry.status ?? "connected",
    instances: [],
    detail: "All keys configured",
    docs_url: entry.docs_url,
    unlocks: entry.unlocks,
  };
}

export function buildFixtureResponse(): ConnectionsResponse {
  const categories: ConnectionCategory[] = FIXTURE_CATALOG.map((cat) => ({
    id: cat.id,
    label: cat.label,
    integrations: cat.items.map(toIntegration),
  }));
  const total = categories.reduce((acc, c) => acc + c.integrations.length, 0);
  const connected = categories.reduce(
    (acc, c) => acc + c.integrations.filter((i) => i.status === "connected").length,
    0,
  );
  return { categories, connected_count: connected, total_count: total };
}
