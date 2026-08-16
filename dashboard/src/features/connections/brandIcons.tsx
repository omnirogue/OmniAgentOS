/**
 * Brand icons — hand-drawn simplified SVG marks for the connections panel.
 *
 * Each glyph uses BRAND-ACCURATE colors on the SVG path only; the surrounding
 * tile chrome stays neutral (Stripe-like restraint per the Design Charter).
 *
 * No external dependencies, no remote asset loads. Everything is inline SVG
 * that renders crisply at any size. If an id has no dedicated glyph the
 * generic "plug" fallback is used — the operator will see a gray plug icon
 * and can mentally substitute.
 *
 * Brand colors sourced from each vendor's guidelines (publicly known):
 *   Anthropic #D97757 · OpenAI #10A37F · xAI #000 · Gemini #4285F4
 *   Kimi #6C66F4 · OpenRouter #0D9488 · Together #0F6FFF · Mistral #F54E42
 *   Replicate #333 · PiedPiper #35BE3B · Gmail #EA4335 · Slack #611F69
 *   Telegram #0088CC · Meta #0668E1 · Stripe #635BFF · PayPal #003087
 *   ElevenLabs #000 · Pexels #05A081 · Pixabay #2EC66C · Cloudflare #F38020
 *   AWS #FF9900 · RunPod #6B46DC · HuggingFace #FFD21E · Teller #0752FE
 */

import type { ReactElement, SVGProps } from "react";

export type BrandIconId =
  | "anthropic"
  | "openai"
  | "xai"
  | "gemini"
  | "kimi"
  | "openrouter"
  | "together"
  | "mistral"
  | "replicate"
  | "piedpiper"
  | "gmail"
  | "slack"
  | "telegram"
  | "meta"
  | "stripe"
  | "paypal"
  | "elevenlabs"
  | "pexels"
  | "pixabay"
  | "cloudflare"
  | "aws"
  | "runpod"
  | "huggingface"
  | "teller"
  | "plug";

type Props = SVGProps<SVGSVGElement> & {
  id: BrandIconId | string;
  /** Size in px; defaults to 24. */
  size?: number;
};

/** Generic plug — the fallback for unknown brand ids. */
function PlugIcon(p: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M9 3v4M15 3v4M7 7h10a1 1 0 0 1 1 1v4a5 5 0 0 1-5 5h-2a5 5 0 0 1-5-5V8a1 1 0 0 1 1-1ZM12 17v4"
        stroke="var(--text-muted)"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Anthropic(p: SVGProps<SVGSVGElement>) {
  // Stylised terracotta "A" — two arcs rising from a shared base.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M4.5 19L12 5l7.5 14M7.5 13h9"
        stroke="#D97757"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function OpenAI(p: SVGProps<SVGSVGElement>) {
  // Simplified hex-ring mark.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M12 3 19 7.5 19 16.5 12 21 5 16.5 5 7.5Z"
        stroke="#10A37F"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="2.4" fill="#10A37F" />
    </svg>
  );
}

function Xai(p: SVGProps<SVGSVGElement>) {
  // "X" mark — xAI's brand.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M5 5l14 14M19 5L5 19"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Gemini(p: SVGProps<SVGSVGElement>) {
  // Sparkle — Gemini's signature glyph.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M12 2v7M12 15v7M2 12h7M15 12h7"
        stroke="#4285F4"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="12" cy="12" r="2.2" fill="#4285F4" />
    </svg>
  );
}

function Kimi(p: SVGProps<SVGSVGElement>) {
  // Stylised "K" — Moonshot/Kimi.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M6 4v16M6 12l7-8M6 12l7 8"
        stroke="#6C66F4"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function OpenRouter(p: SVGProps<SVGSVGElement>) {
  // Two arcs routing — open-source aggregator.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M4 12a8 8 0 0 1 16 0M4 12a8 8 0 0 0 16 0"
        stroke="#0D9488"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <circle cx="12" cy="12" r="1.8" fill="#0D9488" />
    </svg>
  );
}

function Together(p: SVGProps<SVGSVGElement>) {
  // Two overlapping discs — Together.AI.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <circle cx="9.5" cy="12" r="5" stroke="#0F6FFF" strokeWidth="1.6" />
      <circle cx="14.5" cy="12" r="5" stroke="#0F6FFF" strokeWidth="1.6" />
    </svg>
  );
}

function Mistral(p: SVGProps<SVGSVGElement>) {
  // Three horizontal bars — Mistral's striped logo simplification.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <rect x="4" y="5" width="16" height="3" rx="1.2" fill="#F54E42" />
      <rect x="4" y="10.5" width="16" height="3" rx="1.2" fill="#FF8C3A" />
      <rect x="4" y="16" width="16" height="3" rx="1.2" fill="#FFD23F" />
    </svg>
  );
}

function Replicate(p: SVGProps<SVGSVGElement>) {
  // Branching arrows — replicate model inference.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <circle cx="6" cy="12" r="2.4" fill="currentColor" />
      <path
        d="M8.5 12h4l4-5m-4 5 4 5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="18" cy="7" r="1.6" fill="currentColor" />
      <circle cx="18" cy="17" r="1.6" fill="currentColor" />
    </svg>
  );
}

function PiedPiper(p: SVGProps<SVGSVGElement>) {
  // "G" glyph — PiedPiper.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M18 8.5A6.5 6.5 0 1 0 18.5 13H13"
        stroke="#35BE3B"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Gmail(p: SVGProps<SVGSVGElement>) {
  // Simplified "M" envelope.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M3 7l9 7 9-7M3 7v10h18V7"
        stroke="#EA4335"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Slack(p: SVGProps<SVGSVGElement>) {
  // Two coloured dots forming the Slack hashtag-like mark.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <circle cx="8" cy="8" r="2.4" fill="#E01E5A" />
      <circle cx="16" cy="8" r="2.4" fill="#36C5F0" />
      <circle cx="8" cy="16" r="2.4" fill="#2EB67D" />
      <circle cx="16" cy="16" r="2.4" fill="#ECB22E" />
    </svg>
  );
}

function Telegram(p: SVGProps<SVGSVGElement>) {
  // Paper plane — Telegram.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M20 4 4 11l5 2 2 6 3-4 6 4Z"
        fill="#0088CC"
        stroke="#0088CC"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path
        d="M9 13l8-6-5 8"
        stroke="#fff"
        strokeWidth="1.2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Meta(p: SVGProps<SVGSVGElement>) {
  // Infinity loop — Meta.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M4 12c0-3 2-5 4-5s3 2 4 5 2 5 4 5 4-2 4-5-2-5-4-5-3 2-4 5-2 5-4 5-4-2-4-5Z"
        stroke="#0668E1"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Stripe(p: SVGProps<SVGSVGElement>) {
  // "S" glyph — Stripe.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M13.5 7c-1-1-2.5-1.3-4-1A3.5 3.5 0 0 0 8 10c0 2 2 2.5 4 3 2 .5 4 1.2 4 3.5A4 4 0 0 1 11 20c-2 0-3.5-.7-4.5-2M12 4v2m0 12v2"
        stroke="#635BFF"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function PayPal(p: SVGProps<SVGSVGElement>) {
  // Two overlapping "P"s — PayPal.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M7 4h6c2.5 0 4.5 2 4 5s-3 4-5 4H9l-1 5H5Z"
        stroke="#003087"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path
        d="M9 6h6c2.5 0 4.5 2 4 5s-3 4-5 4h-3l-1 5H7Z"
        stroke="#009CDE"
        strokeWidth="1.4"
        strokeLinejoin="round"
        opacity="0.8"
      />
    </svg>
  );
}

function ElevenLabs(p: SVGProps<SVGSVGElement>) {
  // Vertical audio bars — ElevenLabs.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M4 12v0M7 9v6M10 6v12M13 9v6M16 7v10M19 10v4"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Pexels(p: SVGProps<SVGSVGElement>) {
  // Camera aperture — Pexels.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <circle cx="12" cy="12" r="8" stroke="#05A081" strokeWidth="1.6" />
      <path
        d="M12 7l3 3-3 2 3 3-5 1-2-5Z"
        stroke="#05A081"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Pixabay(p: SVGProps<SVGSVGElement>) {
  // Image/leaf — Pixabay.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M5 19l5-7 4 4 2-3 4 6Z"
        stroke="#2EC66C"
        strokeWidth="1.6"
        strokeLinejoin="round"
        fill="none"
      />
      <circle cx="8" cy="9" r="1.8" fill="#2EC66C" />
    </svg>
  );
}

function Cloudflare(p: SVGProps<SVGSVGElement>) {
  // Cloud with arrow — Cloudflare.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M6 17a4 4 0 0 1-.8-7.9A5 5 0 0 1 14.8 7 4.5 4.5 0 0 1 19 12a4 4 0 0 1-3 5Z"
        stroke="#F38020"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path
        d="M8 13h8l-2-2M16 13l-2 2"
        stroke="#F38020"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function AWS(p: SVGProps<SVGSVGElement>) {
  // "A" with smile — AWS.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M6 17l6-12 6 12M8 13h8"
        stroke="#FF9900"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M5 19c3 1.2 9 1.4 14 0"
        stroke="#FF9900"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function RunPod(p: SVGProps<SVGSVGElement>) {
  // GPU chip with dot — RunPod.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <rect
        x="6"
        y="6"
        width="12"
        height="12"
        rx="2"
        stroke="#6B46DC"
        strokeWidth="1.6"
      />
      <circle cx="12" cy="12" r="2.4" fill="#6B46DC" />
      <path
        d="M3 9h2M3 15h2M19 9h2M19 15h2M9 3v2M15 3v2M9 19v2M15 19v2"
        stroke="#6B46DC"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

function HuggingFace(p: SVGProps<SVGSVGElement>) {
  // Smiley — HuggingFace 🤗 simplified.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <circle
        cx="12"
        cy="12"
        r="8"
        stroke="#FFD21E"
        strokeWidth="1.6"
        fill="#FFD21E"
        fillOpacity="0.15"
      />
      <circle cx="9.5" cy="10.5" r="1" fill="#1E1E1E" />
      <circle cx="14.5" cy="10.5" r="1" fill="#1E1E1E" />
      <path
        d="M8.5 14.5c1 1.5 5.5 1.5 7 0"
        stroke="#1E1E1E"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Teller(p: SVGProps<SVGSVGElement>) {
  // "T" with underline — Teller.
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...p}>
      <path
        d="M5 7h14M12 7v12"
        stroke="#0752FE"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M7 20h10"
        stroke="#0752FE"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

const GLYPH_MAP: Record<string, (p: SVGProps<SVGSVGElement>) => ReactElement> = {
  anthropic: Anthropic,
  openai: OpenAI,
  xai: Xai,
  gemini: Gemini,
  kimi: Kimi,
  openrouter: OpenRouter,
  together: Together,
  mistral: Mistral,
  replicate: Replicate,
  piedpiper: PiedPiper,
  gmail: Gmail,
  slack: Slack,
  telegram: Telegram,
  meta: Meta,
  stripe: Stripe,
  paypal: PayPal,
  elevenlabs: ElevenLabs,
  pexels: Pexels,
  pixabay: Pixabay,
  cloudflare: Cloudflare,
  aws: AWS,
  runpod: RunPod,
  huggingface: HuggingFace,
  teller: Teller,
  plug: PlugIcon,
};

/**
 * Brand icon renderer — resolves an Integration.logo id to a coloured SVG.
 * Unknown ids fall back to the generic plug glyph.
 */
export function BrandIcon({ id, size = 24, ...rest }: Props) {
  const Comp = GLYPH_MAP[id] ?? PlugIcon;
  return (
    <Comp
      width={size}
      height={size}
      role="img"
      aria-label={`${id} brand icon`}
      {...rest}
    />
  );
}
