import { describe, expect, it } from "vitest";
import type { Session } from "../../lib/contracts";
import { effectiveCompany, groupSessionsByCompany, sessionsNeedingAttention } from "./grouping";

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "ses_1",
    source: "bridge",
    project_dir: "/workspace/x",
    provider: "claude",
    state: "running",
    model: null,
    title: null,
    cost_usd: null,
    last_activity_at: null,
    created_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

describe("effectiveCompany", () => {
  it("prefers company_override over the collector-assigned company", () => {
    expect(effectiveCompany({ company: "AcmeUni", company_override: "Globex" })).toBe("Globex");
  });

  it("falls back to company when there is no override", () => {
    expect(effectiveCompany({ company: "AcmeUni", company_override: null })).toBe("AcmeUni");
  });

  it("treats a blank override as absent", () => {
    expect(effectiveCompany({ company: "AcmeUni", company_override: "   " })).toBe("AcmeUni");
  });

  it("returns null when both fields are absent", () => {
    expect(effectiveCompany({ company: null, company_override: null })).toBeNull();
  });
});

describe("groupSessionsByCompany", () => {
  it("orders known companies AcmeUni, Globex, Initech, Hooli, Ops before a trailing Other, dropping empty groups", () => {
    const sessions = [
      session({ id: "s_ops", company: "Ops" }),
      session({ id: "s_acmeuni", company: "AcmeUni" }),
      session({ id: "s_hooli", company: "Hooli" }),
      session({ id: "s_other", company: "Acme Widgets" }),
      session({ id: "s_none" }),
    ];
    const groups = groupSessionsByCompany(sessions);
    expect(groups.map((g) => g.company)).toEqual(["AcmeUni", "Hooli", "Ops", "Other"]);
    expect(groups.find((g) => g.company === "Other")?.sessions.map((s) => s.id)).toEqual(["s_other", "s_none"]);
    // Initech and Globex had zero sessions in this fixture — they must not appear.
    expect(groups.some((g) => g.company === "Initech" || g.company === "Globex")).toBe(false);
  });

  it("matches known company names case-insensitively", () => {
    const groups = groupSessionsByCompany([session({ company: "globex" })]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.company).toBe("Globex");
  });

  it("prefers a manual company_override over the collector-assigned company when grouping", () => {
    const groups = groupSessionsByCompany([session({ company: "AcmeUni", company_override: "Initech" })]);
    expect(groups.map((g) => g.company)).toEqual(["Initech"]);
  });

  it("returns no groups for an empty roster", () => {
    expect(groupSessionsByCompany([])).toEqual([]);
  });
});

describe("sessionsNeedingAttention", () => {
  it("keeps only sessions with attention_state === needs_input", () => {
    const sessions = [
      session({ id: "a", attention_state: "needs_input" }),
      session({ id: "b", attention_state: "finished" }),
      session({ id: "c", attention_state: null }),
      session({ id: "d" }),
    ];
    expect(sessionsNeedingAttention(sessions).map((s) => s.id)).toEqual(["a"]);
  });

  it("returns an empty array when nothing needs attention", () => {
    expect(sessionsNeedingAttention([session({ attention_state: "finished" }), session({ id: "d" })])).toEqual([]);
  });
});
