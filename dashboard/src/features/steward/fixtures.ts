/** Synthetic, API-shaped fixtures for the Steward dashboard. Enable locally with
 * NEXT_PUBLIC_USE_STEWARD_FIXTURES=true (mirrors features/collab/fixtures.ts). Default
 * OFF: production always talks to the real /api/{briefings,goals,comms,suggestions,alerts,voice}
 * routes. */
import type {
  Alert,
  ApproveSuggestionResult,
  Briefing,
  CommsMessage,
  CommsSource,
  Goal,
  GoalDetail,
  Suggestion,
  VoiceProviderStatus,
} from "./types";

export const USE_STEWARD_FIXTURES = process.env.NEXT_PUBLIC_USE_STEWARD_FIXTURES === "true";

export const FIXTURE_BRIEFING: Briefing = {
  id: "brf_2026-07-13",
  briefing_date: "2026-07-13",
  vault_path: "briefings/2026/07/13.md",
  summary: {
    subject: "Daily briefing — 2026-07-13",
    headline: "12 messages, 4 completed runs, 1 open alert",
    sections: [
      {
        title: "Metrics and urgent items",
        body:
          "- goal: increase-revenue; metric: net_revenue_usd; latest: 12000; previous: 10600; delta_pct: 13.2\n\nPending approvals: 0\nOpen alerts: 1\nOpen suggestions: 2",
      },
      {
        title: "Communications digest",
        body: "- ops@acme.example — Payment retries succeeding\n\"Retried failed charges overnight, 4 of 6 recovered.\"",
      },
      {
        title: "Operations",
        body: "Runs completed: 4; failed: 0; cost: $0.6420. Promoted facts: Retrying without changing inputs rarely helps.",
      },
    ],
    composed_by: "llm",
  },
  deliveries: [
    { channel: "slack", ok: true, detail: "delivered", artifact_id: null },
    { channel: "voice", ok: true, detail: "synthesized", artifact_id: "voice_2026-07-13" },
  ],
  voice_artifact_id: "voice_2026-07-13",
  acked_at: null,
};

export const FIXTURE_BRIEFING_HISTORY: Briefing[] = [
  FIXTURE_BRIEFING,
  {
    ...FIXTURE_BRIEFING,
    id: "brf_2026-07-12",
    briefing_date: "2026-07-12",
    acked_at: "2026-07-12T08:05:00Z",
    summary: { ...FIXTURE_BRIEFING.summary, headline: "9 messages, 3 completed runs, 0 open alerts" },
  },
];

export const FIXTURE_GOALS: Goal[] = [
  {
    id: "increase-revenue",
    name: "Increase revenue",
    description: "Grow net Stripe revenue while keeping payment failures low.",
    discipline_id: "revenue",
    north_star: { source: "stripe", metric: "net_revenue_usd" },
    target: { value: 15000 },
    keywords: ["revenue", "stripe", "payments"],
    priority: 10,
    status: "active",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-07-13T07:30:00Z",
    latest: {
      net_revenue_usd: { id: 501, goal_id: "increase-revenue", source: "stripe", metric: "net_revenue_usd", value: 12000, unit: "usd", window: "day", captured_at: "2026-07-13T06:00:00Z", meta: {} },
      payment_failures: { id: 502, goal_id: "increase-revenue", source: "stripe", metric: "payment_failures", value: 2, unit: "count", window: "day", captured_at: "2026-07-13T06:00:00Z", meta: {} },
    },
    trend: {
      net_revenue_usd: Array.from({ length: 7 }, (_, i) => ({
        id: 400 + i,
        goal_id: "increase-revenue",
        source: "stripe",
        metric: "net_revenue_usd",
        value: 9000 + i * 500,
        unit: "usd",
        window: "day",
        captured_at: `2026-07-${(7 + i).toString().padStart(2, "0")}T06:00:00Z`,
        meta: {},
      })),
      payment_failures: [],
    },
  },
  {
    id: "improve-ad-roi",
    name: "Improve ad ROI",
    description: "Keep Meta ROAS above the profitability floor.",
    discipline_id: "ad-roi",
    north_star: { source: "meta", metric: "roas" },
    target: { value: 2.0 },
    keywords: ["meta", "ads", "roas"],
    priority: 20,
    status: "active",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-07-13T07:30:00Z",
    latest: {
      spend_usd: { id: 601, goal_id: "improve-ad-roi", source: "meta", metric: "spend_usd", value: 2500, unit: "usd", window: "day", captured_at: "2026-07-13T06:00:00Z", meta: {} },
      roas: { id: 602, goal_id: "improve-ad-roi", source: "meta", metric: "roas", value: 1.4, unit: "ratio", window: "day", captured_at: "2026-07-13T06:00:00Z", meta: {} },
    },
    trend: {
      roas: Array.from({ length: 7 }, (_, i) => ({
        id: 700 + i,
        goal_id: "improve-ad-roi",
        source: "meta",
        metric: "roas",
        value: 2.1 - i * 0.1,
        unit: "ratio",
        window: "day",
        captured_at: `2026-07-${(7 + i).toString().padStart(2, "0")}T06:00:00Z`,
        meta: {},
      })),
      spend_usd: [],
    },
  },
];

export const FIXTURE_GOAL_DETAILS: Record<string, GoalDetail> = Object.fromEntries(
  FIXTURE_GOALS.map((goal) => [
    goal.id,
    {
      ...goal,
      facts: [{ fact_id: 61, statement: "State assumptions before proposing an irreversible operational action." }],
      suggestions: [],
      recent_runs: [
        { id: "run_demo_1", task_id: "task_demo_1", state: "completed", harness: "cli-claude", model: "sonnet", queued_at: "2026-07-13T06:01:00Z", started_at: "2026-07-13T06:01:05Z", finished_at: "2026-07-13T06:03:40Z", cost_usd: 0.21 },
      ],
    },
  ]),
);

export const FIXTURE_COMMS_SOURCES: CommsSource[] = [
  { name: "gmail-ops", kind: "email", status: "connected", config: {}, last_poll_at: "2026-07-13T07:00:00Z", last_error: null },
  { name: "slack-alerts", kind: "slack", status: "connected", config: {}, last_poll_at: "2026-07-13T07:00:00Z", last_error: null },
];

export const FIXTURE_COMMS_MESSAGES: CommsMessage[] = [
  { id: 301, source: "gmail-ops", external_id: "m-301", thread_id: "t-1", sender: "ops@acme.example", recipients: ["owner@acmeuni.example"], sent_at: "2026-07-13T06:40:00Z", subject: "Payment retries succeeding", body_text: "Retried failed charges overnight, 4 of 6 recovered.", attachments: [], kb_status: "extracted", episode_id: 14, created_at: "2026-07-13T06:41:00Z" },
  { id: 300, source: "slack-alerts", external_id: "m-300", thread_id: "t-1", sender: "#ops-alerts", recipients: [], sent_at: "2026-07-13T05:12:00Z", subject: "ROAS dipped below floor", body_text: "Meta ROAS is 1.4, below the 2.0 floor for 3 consecutive days.", attachments: [], kb_status: "none", episode_id: null, created_at: "2026-07-13T05:13:00Z" },
];

export const FIXTURE_SUGGESTIONS: Suggestion[] = [
  {
    id: "sug_demo_1",
    title: "Investigate revenue dip",
    rationale: "Net revenue trended down 3 consecutive days before today's recovery.",
    evidence: [{ metric: "net_revenue_usd", delta_pct: -8.4 }],
    proposed_plan: {
      prompt: "Analyze the last 7 days of Stripe revenue read-only.",
      harness: "cli-claude",
      action_class: "read_only",
    },
    risk_class: "read_only",
    source: "steward.suggest.revenue_downtrend",
    goal_id: "increase-revenue",
    alert_id: null,
    outcome: {},
    state: "open",
    created_at: "2026-07-13T06:05:00Z",
    decided_at: null,
    decided_by: null,
    task_id: null,
    run_id: null,
  },
  {
    id: "sug_demo_2",
    title: "Review underperforming ad sets",
    rationale: "Suggested by alert rule roas_floor.",
    evidence: [{ metric: "roas", value: 1.4 }],
    proposed_plan: {
      prompt: "Analyze yesterday's Meta ad performance read-only.",
      harness: "cli-claude",
      action_class: "read_only",
    },
    risk_class: "read_only",
    source: "alerts",
    goal_id: "improve-ad-roi",
    alert_id: 201,
    outcome: {},
    state: "open",
    created_at: "2026-07-13T05:15:00Z",
    decided_at: null,
    decided_by: null,
    task_id: null,
    run_id: null,
  },
  {
    id: "sug_demo_3",
    title: "Pause underperforming Meta ad set",
    rationale: "ROAS has stayed below the 2.0 floor for 3 consecutive days with no recovery signal.",
    evidence: [{ metric: "roas", value: 1.4, floor: 2.0, days_below_floor: 3 }],
    proposed_plan: {
      prompt: "Pause the 'Retarget-Campaign-A' Meta ad set (id campaign_example_1) — spend has exceeded return for 3 days.",
      harness: "cli-claude",
      action_class: "external_reversible",
    },
    risk_class: "external_reversible",
    source: "alerts",
    goal_id: "improve-ad-roi",
    alert_id: 201,
    outcome: {},
    state: "open",
    created_at: "2026-07-13T05:20:00Z",
    decided_at: null,
    decided_by: null,
    task_id: null,
    run_id: null,
  },
];

export const FIXTURE_ALERTS: Alert[] = [
  { id: 201, rule: "roas_floor", severity: "critical", title: "ROAS below floor", body: "Meta ROAS is 1.4, below the 2.0 floor for 3 consecutive days.", evidence: { roas: 1.4, floor: 2.0 }, cooldown_key: "roas_floor", state: "open", created_at: "2026-07-13T05:12:00Z", acked_at: null },
  { id: 200, rule: "payment_failures", severity: "high", title: "Payment failure burst", body: "3 consecutive Stripe charge failures in the last hour.", evidence: { count: 3 }, cooldown_key: "payment_failures", state: "acked", created_at: "2026-07-12T22:00:00Z", acked_at: "2026-07-12T22:05:00Z" },
];

export const FIXTURE_VOICE_PROVIDERS: VoiceProviderStatus[] = [
  { provider: "elevenlabs", status: "ready", detail: "API key configured" },
  { provider: "xai", status: "no_key", detail: "XAI_API_KEY not set" },
];

/** Mutate the in-session fixture roster so approve/dismiss/ack feel real in demo mode
 * (mirrors features/collab/fixtures.ts::pushFixtureAgent). */
export function decideFixtureSuggestion(
  id: string,
  state: "approved" | "dismissed",
  decidedBy: string,
  reason?: string,
): Suggestion {
  const index = FIXTURE_SUGGESTIONS.findIndex((row) => row.id === id);
  const current = index >= 0 ? FIXTURE_SUGGESTIONS[index]! : FIXTURE_SUGGESTIONS[0]!;
  const decided: Suggestion = {
    ...current,
    id,
    state,
    decided_by: decidedBy,
    decided_at: new Date().toISOString(),
    outcome: reason ? { dismiss_reason: reason } : current.outcome,
    task_id: state === "approved" ? `task_fixture_${id}` : current.task_id,
    run_id: state === "approved" ? `run_fixture_${id}` : current.run_id,
  };
  if (index >= 0) FIXTURE_SUGGESTIONS[index] = decided;
  return decided;
}

export function approveFixtureSuggestion(id: string, approvedBy: string): ApproveSuggestionResult {
  const suggestion = decideFixtureSuggestion(id, "approved", approvedBy);
  return { suggestion, task_id: suggestion.task_id ?? `task_fixture_${id}`, run_id: suggestion.run_id ?? `run_fixture_${id}` };
}

export function ackFixtureAlert(id: number, by = "operator"): Alert {
  const index = FIXTURE_ALERTS.findIndex((row) => row.id === id);
  const current = index >= 0 ? FIXTURE_ALERTS[index]! : FIXTURE_ALERTS[0]!;
  const acked: Alert = { ...current, id, state: "acked", acked_at: new Date().toISOString() };
  if (index >= 0) FIXTURE_ALERTS[index] = acked;
  void by;
  return acked;
}

export function ackFixtureBriefing(id: string): Briefing {
  return { ...FIXTURE_BRIEFING, id, acked_at: new Date().toISOString() };
}
