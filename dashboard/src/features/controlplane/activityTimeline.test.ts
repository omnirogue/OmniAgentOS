/**
 * Pure projection tests for the readable agent activity timeline.
 *
 * Spec for implementer (product module expected at `./activityTimeline.ts`):
 * - `formatActivityTimestamp(iso)` — deterministic UTC label; never wall-clock.
 * - `deriveActivityPhase(action, payload)` — prefer payload.phase; else map action.
 * - `projectActivityTimelineEntry(event)` — one readable row from a snapshot event.
 * - `projectActivityTimeline(events)` — ascending by event id (cursor order).
 *
 * Each row surfaces: role, model/account, timestamp, phase, concise action/result,
 * GitHub-only evidence links. Raw payload is kept for a collapsed disclosure —
 * never parsed as HTML by the consumer.
 */
import { describe, expect, it } from "vitest";
import {
  deriveActivityPhase,
  formatActivityTimestamp,
  projectActivityTimeline,
  projectActivityTimelineEntry,
} from "./activityTimeline";

function event(
  overrides: {
    id?: number;
    action?: string | null;
    created_at?: string | null;
    payload?: Record<string, unknown>;
  } = {},
) {
  return {
    id: overrides.id ?? 1,
    action: overrides.action === undefined ? "task_assigned" : overrides.action,
    created_at: overrides.created_at === undefined ? "2026-08-07T12:00:00Z" : overrides.created_at,
    payload: overrides.payload ?? {},
  };
}

describe("formatActivityTimestamp", () => {
  it("formats a fixed ISO instant as a stable UTC label (no locale / wall-clock drift)", () => {
    expect(formatActivityTimestamp("2026-08-07T12:00:00Z")).toBe("2026-08-07 12:00:00 UTC");
  });

  it("returns an em-dash for missing or unparseable timestamps", () => {
    expect(formatActivityTimestamp(null)).toBe("—");
    expect(formatActivityTimestamp(undefined)).toBe("—");
    expect(formatActivityTimestamp("")).toBe("—");
    expect(formatActivityTimestamp("not-a-date")).toBe("—");
  });
});

describe("deriveActivityPhase", () => {
  it("prefers an explicit payload.phase when present", () => {
    expect(deriveActivityPhase("task_assigned", { phase: "repair" })).toBe("repair");
  });

  it("maps common swarm/engine actions to readable phases when phase is absent", () => {
    expect(deriveActivityPhase("plan_created", {})).toBe("planning");
    expect(deriveActivityPhase("run_started", {})).toBe("running");
    expect(deriveActivityPhase("task_assigned", {})).toBe("running");
    expect(deriveActivityPhase("worker_spawned", {})).toBe("running");
    expect(deriveActivityPhase("attempt.started", {})).toBe("running");
    expect(deriveActivityPhase("review_confirmed", {})).toBe("reviewing");
    expect(deriveActivityPhase("review_denied", {})).toBe("reviewing");
    expect(deriveActivityPhase("task_completed", {})).toBe("done");
    expect(deriveActivityPhase("run_completed", {})).toBe("done");
    expect(deriveActivityPhase("run_failed", {})).toBe("failed");
    expect(deriveActivityPhase("rate_limit", {})).toBe("blocked");
    expect(deriveActivityPhase("task_blocked", {})).toBe("blocked");
    expect(deriveActivityPhase("approval_parked", {})).toBe("parked");
  });

  it("returns null for unknown actions without a payload phase", () => {
    expect(deriveActivityPhase("future.unknown_action", {})).toBeNull();
    expect(deriveActivityPhase(null, {})).toBeNull();
  });
});

describe("projectActivityTimelineEntry", () => {
  it("projects role, model, account, timestamp, phase, action, and concise result", () => {
    const row = projectActivityTimelineEntry(
      event({
        id: 42,
        action: "task_assigned",
        created_at: "2026-08-07T12:00:00Z",
        payload: {
          role: "implementer",
          model: "grok-4.5",
          account: "codex-2",
          provider: "codex",
          status: "running",
          reason: "slot admitted",
          phase: "running",
          note: "started work on adapter",
        },
      }),
    );

    expect(row.id).toBe(42);
    expect(row.timestampIso).toBe("2026-08-07T12:00:00Z");
    expect(row.timestampLabel).toBe("2026-08-07 12:00:00 UTC");
    expect(row.role).toBe("implementer");
    expect(row.model).toBe("grok-4.5");
    expect(row.account).toBe("codex-2");
    expect(row.modelAccountLabel).toBe("grok-4.5 · codex-2");
    expect(row.phase).toBe("running");
    expect(row.action).toBe("task_assigned");
    // Concise result prefers status, then reason, then note — not a raw dump.
    expect(row.result).toBe("running");
    expect(row.summary).toMatch(/task_assigned/);
    expect(row.summary).toMatch(/running/);
    expect(row.hasRawPayload).toBe(true);
    expect(row.rawPayload).toMatchObject({ role: "implementer", model: "grok-4.5" });
  });

  it("builds modelAccountLabel from whichever of model/account is present", () => {
    expect(
      projectActivityTimelineEntry(event({ payload: { model: "gpt-5.6-sol" } })).modelAccountLabel,
    ).toBe("gpt-5.6-sol");
    expect(
      projectActivityTimelineEntry(event({ payload: { account: "claude-1" } })).modelAccountLabel,
    ).toBe("claude-1");
    expect(projectActivityTimelineEntry(event({ payload: {} })).modelAccountLabel).toBe("—");
  });

  it("never promotes account_id into the readable account field (label only)", () => {
    const row = projectActivityTimelineEntry(
      event({
        payload: { account_id: "private-account-id", account: "codex-2" },
      }),
    );
    expect(row.account).toBe("codex-2");
    // The raw payload may still carry account_id if the server sent it, but
    // the readable field must never treat account_id as the operator-facing label.
    expect(row.modelAccountLabel).not.toContain("private-account-id");
  });

  it("keeps only https://github.com/ hrefs; refused urls become inert label-only entries", () => {
    const row = projectActivityTimelineEntry(
      event({
        action: "evidence.reported",
        payload: {
          evidence_links: [
            {
              label: "aaa1111",
              url: "https://github.com/acme/widgets/commit/aaa1111",
            },
            {
              label: "evil",
              url: "https://evil.example.com/aaa",
            },
            {
              label: "no-url",
              url: null,
            },
          ],
        },
      }),
    );
    expect(row.evidenceLinks).toEqual([
      { label: "aaa1111", href: "https://github.com/acme/widgets/commit/aaa1111" },
      { label: "evil", href: null },
      { label: "no-url", href: null },
    ]);
  });

  // F005 (crit-20260813T115400Z receipt): a URL refusal must retain inert
  // evidence instead of becoming identical to a genuinely empty evidence list.
  it("keeps a refused link distinguishable from no evidence at all", () => {
    const base = { action: "evidence.reported" };
    const healthyEmpty = projectActivityTimelineEntry(event({ ...base, payload: {} }))
      .evidenceLinks;
    const refused = projectActivityTimelineEntry(
      event({
        ...base,
        payload: { evidence_links: [{ label: "blocked destination", url: null }] },
      }),
    ).evidenceLinks;
    expect(healthyEmpty).toEqual([]);
    expect(refused).not.toEqual(healthyEmpty);
    expect(refused).toEqual([{ label: "blocked destination", href: null }]);
  });

  it("drops evidence_links entries with nothing displayable (no label, name, or valid url)", () => {
    const row = projectActivityTimelineEntry(
      event({ payload: { evidence_links: [{ url: null }, { note: "x" }] } }),
    );
    expect(row.evidenceLinks).toEqual([]);
  });

  it("also accepts evidence_links entries keyed as name instead of label", () => {
    const row = projectActivityTimelineEntry(
      event({
        payload: {
          evidence_links: [
            { name: "coverage", url: "https://github.com/acme/widgets/actions/runs/1" },
          ],
        },
      }),
    );
    expect(row.evidenceLinks).toEqual([
      { label: "coverage", href: "https://github.com/acme/widgets/actions/runs/1" },
    ]);
  });

  it("falls back to reason/note for result when status is absent", () => {
    expect(
      projectActivityTimelineEntry(event({ payload: { reason: "rate limited" } })).result,
    ).toBe("rate limited");
    expect(
      projectActivityTimelineEntry(event({ payload: { note: "spawned worker" } })).result,
    ).toBe("spawned worker");
    expect(projectActivityTimelineEntry(event({ payload: {} })).result).toBeNull();
  });

  it("treats missing action as the literal fallback 'event'", () => {
    expect(projectActivityTimelineEntry(event({ action: null })).action).toBe("event");
    expect(projectActivityTimelineEntry(event({ action: "" })).action).toBe("event");
  });

  it("marks hasRawPayload false only when the payload object is empty", () => {
    expect(projectActivityTimelineEntry(event({ payload: {} })).hasRawPayload).toBe(false);
    expect(
      projectActivityTimelineEntry(event({ payload: { task_id: "t1" } })).hasRawPayload,
    ).toBe(true);
  });
});

describe("projectActivityTimeline", () => {
  it("sorts rows by event id ascending (cursor order), independent of input order", () => {
    const rows = projectActivityTimeline([
      event({ id: 3, action: "run_completed" }),
      event({ id: 1, action: "run_started" }),
      event({ id: 2, action: "task_assigned" }),
    ]);
    expect(rows.map((r) => r.id)).toEqual([1, 2, 3]);
    expect(rows.map((r) => r.action)).toEqual(["run_started", "task_assigned", "run_completed"]);
  });

  it("returns an empty list for empty input", () => {
    expect(projectActivityTimeline([])).toEqual([]);
  });
});
