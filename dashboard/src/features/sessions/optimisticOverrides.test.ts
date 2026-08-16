import { describe, expect, it } from "vitest";
import type { Session } from "../../lib/contracts";
import { applyOverride, resolveSessionOverrides, settleOverride, type SessionOverrides } from "./optimisticOverrides";

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "ses_1",
    source: "bridge",
    project_dir: "/workspace/x",
    provider: "claude",
    state: "running",
    model: null,
    title: "Original title",
    cost_usd: null,
    last_activity_at: null,
    created_at: "2026-08-01T00:00:00Z",
    company: "AcmeUni",
    company_override: null,
    ...overrides,
  };
}

describe("settleOverride: concurrent edits on the same session (finding 2a regression)", () => {
  it("settling a title save preserves a still-in-flight company edit on the same row", () => {
    let overrides: SessionOverrides = {};
    overrides = applyOverride(overrides, "ses_1", "title", "Title A", 1); // title save starts
    overrides = applyOverride(overrides, "ses_1", "company_override", "Globex", 2); // company save starts while title is still saving

    // Title save completes first.
    overrides = settleOverride(overrides, "ses_1", "title", 1);

    expect(overrides["ses_1"]).toEqual({ company_override: { value: "Globex", requestId: 2 } });
    expect(resolveSessionOverrides(session(), overrides).company_override).toBe("Globex");
  });

  it("removes the session entry entirely once every field has settled", () => {
    let overrides: SessionOverrides = {};
    overrides = applyOverride(overrides, "ses_1", "title", "Title A", 1);
    overrides = settleOverride(overrides, "ses_1", "title", 1);
    expect(overrides).toEqual({});
  });

  it("does not let an older request's settle clobber a newer edit on the SAME field", () => {
    let overrides: SessionOverrides = {};
    overrides = applyOverride(overrides, "ses_1", "title", "First edit", 1);
    // A second edit on the same field starts before the first save resolves.
    overrides = applyOverride(overrides, "ses_1", "title", "Second edit", 2);

    // The FIRST save's request settles late — must not erase the second edit.
    overrides = settleOverride(overrides, "ses_1", "title", 1);

    expect(overrides["ses_1"]?.title).toEqual({ value: "Second edit", requestId: 2 });
  });

  it("settling an unknown session/field is a no-op", () => {
    const overrides: SessionOverrides = {};
    expect(settleOverride(overrides, "ses_1", "title", 1)).toBe(overrides);
  });
});

describe("resolveSessionOverrides", () => {
  it("returns the original session object when there is no override", () => {
    const s = session();
    expect(resolveSessionOverrides(s, {})).toBe(s);
  });

  it("merges a pending title override onto the session for display", () => {
    const overrides: SessionOverrides = { ses_1: { title: { value: "Optimistic title", requestId: 1 } } };
    expect(resolveSessionOverrides(session(), overrides).title).toBe("Optimistic title");
  });
});
