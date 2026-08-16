/**
 * OmniAgentOS design tokens — FROZEN (H2 Wave 0).
 *
 * The single source of visual truth. U01 implements primitives from these; U02–U05
 * consume ONLY these tokens + primitives — never raw hex, never ad-hoc spacing — so
 * the whole app reads as one designed system. Aesthetic: a calm, precise, dark-first
 * "mission control for a research lab" — deep neutral canvas, restrained luminous
 * accents, generous space, one expressive data-accent ramp for ELO/metrics.
 *
 * Delivered as CSS custom properties (see design/theme.css, applied by U01) AND these
 * TS constants for charts/inline logic. Light theme is a first-class sibling, not an
 * afterthought — every semantic token is defined in both.
 */

export const font = {
  sans: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  mono: "'JetBrains Mono', 'SF Mono', ui-monospace, monospace",
  // type scale (rem) — 1.20 minor-third; sizes are semantic, not pixel-poked in features
  size: {
    display: "2.25rem", h1: "1.75rem", h2: "1.375rem", h3: "1.125rem",
    body: "0.9375rem", small: "0.8125rem", micro: "0.6875rem",
  },
  weight: { regular: 400, medium: 500, semibold: 600, bold: 700 },
  leading: { tight: 1.2, normal: 1.5, relaxed: 1.7 },
  tracking: { tight: "-0.02em", normal: "0", wide: "0.04em" },
} as const;

export const space = {
  // 4px base; use these, never arbitrary px
  x0: "0", x1: "0.25rem", x2: "0.5rem", x3: "0.75rem", x4: "1rem",
  x5: "1.5rem", x6: "2rem", x8: "3rem", x10: "4rem", x12: "6rem",
} as const;

export const radius = {
  sm: "6px", md: "10px", lg: "14px", xl: "20px", pill: "999px",
} as const;

/** Elevation via layered shadows tuned for a dark canvas (soft, not heavy). */
export const shadow = {
  sm: "0 1px 2px rgba(0,0,0,.28)",
  md: "0 4px 16px rgba(0,0,0,.32)",
  lg: "0 12px 40px rgba(0,0,0,.40)",
  glow: "0 0 0 1px var(--accent-ring), 0 0 24px -6px var(--accent-glow)",
} as const;

export const motion = {
  fast: "120ms", base: "200ms", slow: "360ms",
  ease: "cubic-bezier(.2,.7,.2,1)",       // decisive, slightly overshooting
  easeOut: "cubic-bezier(0,.6,.3,1)",
  spring: "cubic-bezier(.34,1.56,.64,1)", // for delightful confirmations
} as const;

/**
 * Semantic color — reference by role, not value. Raw ramps stay here; features use the
 * CSS var names (theme.css maps these per light/dark). Contrast: body/UI text pairs
 * meet WCAG AA on their backgrounds in BOTH themes (validated by U01).
 */
export const color = {
  dark: {
    canvas: "#0b0e14", surface: "#12161f", surfaceRaised: "#171c27",
    overlay: "#1d2431", border: "#232b3a", borderStrong: "#33405a",
    text: "#e6e9ef", textMuted: "#9aa4b6", textFaint: "#6b7688",
    accent: "#6ea8fe", accentText: "#0b0e14", accentRing: "#2a4a7f", accentGlow: "#6ea8fe",
    // domain semantics
    champion: "#f5c451", challenger: "#7ee0c0", promote: "#3fb970", reject: "#e0607a",
    running: "#6ea8fe", paused: "#e0607a", awaiting: "#f0a35e", validating: "#b48ef0",
    ok: "#3fb970", warn: "#e6b450", danger: "#f0616e",
  },
  light: {
    canvas: "#f6f7f9", surface: "#ffffff", surfaceRaised: "#fbfcfd",
    overlay: "#f8fafc", border: "#e3e7ee", borderStrong: "#c9d2df",
    text: "#141821", textMuted: "#525c6e", textFaint: "#79839a",
    accent: "#2f6bd8", accentText: "#ffffff", accentRing: "#b7ccf0", accentGlow: "#2f6bd8",
    champion: "#b8860b", challenger: "#0f9b78", promote: "#1a8f52", reject: "#c23d5a",
    running: "#2f6bd8", paused: "#c23d5a", awaiting: "#b56b18", validating: "#7a4fc0",
    ok: "#1a8f52", warn: "#a9791a", danger: "#c8404d",
  },
} as const;

/**
 * Categorical + sequential data palettes for charts/ELO/leaderboards (dataviz).
 * Color-vision-safe, distinct at a glance, ordered by luminance for the sequential ramp.
 * Feature charts pull from here so every visualization matches the system.
 */
export const dataviz = {
  categorical: ["#6ea8fe", "#7ee0c0", "#f5c451", "#b48ef0", "#f0888e", "#5ecbdd", "#e6a15e", "#9aa4b6"],
  sequential: ["#12305e", "#1f4c86", "#2f6bd8", "#5b8fe8", "#8fb4f2", "#c3d8fa"],
  diverging: ["#e0607a", "#e6a19a", "#dfe3ea", "#8fd0bd", "#3fb970"], // reject → neutral → promote
} as const;

export const zIndex = { base: 0, sticky: 20, dropdown: 40, dialog: 60, toast: 80, palette: 100 } as const;

export const layout = { navWidth: "15rem", contentMax: "112rem", headerHeight: "3.5rem" } as const;

/**
 * SEMANTIC domain tokens (design review HD-014) — reference these by MEANING so
 * U02 (experiments), U03 (leaderboard/ELO), U05 (ops) render the same concept the same
 * way. Values map to the CSS vars in theme.css (per light/dark). Never re-pick a color
 * for "promote"/"win"/"champion" in a feature package.
 */
export const semantic = {
  arm: { champion: "var(--champion)", challenger: "var(--challenger)" },
  disposition: {
    promote: "var(--promote)", reject: "var(--reject)",
    invalid: "var(--text-faint)", human_review: "var(--awaiting)",
  },
  result: { win: "var(--promote)", loss: "var(--reject)", draw: "var(--text-muted)" },
  runState: {
    queued: "var(--text-muted)", running: "var(--running)", awaiting_approval: "var(--awaiting)",
    validating: "var(--validating)", paused: "var(--paused)",
    completed: "var(--ok)", failed: "var(--danger)", cancelled: "var(--text-faint)",
  },
  /** Improvement risk levels L1–L4 (V2 reliability). CSS vars set in theme.css. */
  risk: {
    L1: "var(--risk-l1)",
    L2: "var(--risk-l2)",
    L3: "var(--risk-l3)",
    L4: "var(--risk-l4)",
  },
  /** System health states for reliability tiles / watch heartbeat. */
  health: {
    healthy: "var(--health-healthy)",
    degraded: "var(--health-degraded)",
    critical: "var(--health-critical)",
    recovering: "var(--health-recovering)",
  },
  // ELO ramp: map a rating to a dataviz.sequential stop for consistent leaderboard shading
  eloStops: [900, 1000, 1100, 1200, 1300, 1400] as const,
} as const;

/**
 * CHART KIT contract (HD-014): U01 ships these components; U02/U03/U05 import them with
 * these exact prop shapes so every chart matches. No feature package hand-rolls SVG/canvas
 * or picks chart colors — series colors come from `dataviz`. Follows the dataviz skill.
 *   <LineChart series={{name,points:[{x,y,ciLow?,ciHigh?}]}[]} />   // CI band when ci* present
 *   <BarChart bars={{label,value,tone?}[]} />
 *   <Bracket rounds={Match[][]} />                                  // tournament bracket
 *   <Sparkline points={number[]} />
 *   <Stat label value delta? tone? />                              // KPI tile
 *   plus <Empty/> <Loading/> <ErrorState/> <Tooltip/> — every data view uses these 4 states.
 */
export const CHART_KIT = [
  "LineChart", "BarChart", "Bracket", "Sparkline", "Stat", "Empty", "Loading", "ErrorState", "Tooltip",
] as const;

export type ThemeName = "dark" | "light";
