import { describe, expect, it } from "vitest";
import {
  NO_RECOMMENDATION_SENTINEL,
  normalizeAvailableAction,
  normalizeDecision,
  normalizeRecommended,
} from "./api";
import { groupDecisions } from "./hooks";
import type { Decision } from "./types";

describe("normalizeRecommended (invariant 1 — recommendation always visible)", () => {
  it.each([undefined, {}, { human_line: "" }, { human_line: "   " }])(
    "renders a loud sentinel and flags missing when no concrete recommendation is present (%j)",
    (input) => {
      const result = normalizeRecommended(input);
      expect(result.human_line).toBe(NO_RECOMMENDATION_SENTINEL);
      expect(result.missing).toBe(true);
    },
  );

  it("preserves a real recommendation and is not flagged missing", () => {
    const result = normalizeRecommended({ kind: "send_email", human_line: "Update the payment method on Stripe now", params: { x: 1 } });
    expect(result.human_line).toBe("Update the payment method on Stripe now");
    expect(result.missing).toBe(false);
    expect(result.params).toEqual({ x: 1 });
  });
});

describe("normalizeAvailableAction (server-curated, rendered verbatim)", () => {
  it("accepts the canonical object form", () => {
    expect(normalizeAvailableAction({ action: "delegate", label: "Delegate", target: null })).toEqual({
      action: "delegate",
      label: "Delegate",
      target: null,
    });
  });

  it("accepts a bare verb string and supplies a fallback label", () => {
    expect(normalizeAvailableAction("edit")).toEqual({ action: "edit", label: "Edit", target: null });
  });

  it("splits a verb:target string form", () => {
    expect(normalizeAvailableAction("defer:machine")).toEqual({
      action: "defer",
      label: "Defer to machine",
      target: "machine",
    });
  });

  it("drops an unknown verb rather than offering a button it cannot render", () => {
    expect(normalizeAvailableAction("nuke")).toBeNull();
    expect(normalizeAvailableAction({ action: "nuke", label: "Nuke" })).toBeNull();
  });
});

describe("normalizeDecision", () => {
  it("fills defensive defaults and never carries an owner field", () => {
    const decision = normalizeDecision({ id: "dcn_1", title: "Vendor contract" });
    expect(decision.id).toBe("dcn_1");
    expect(decision.classification).toBe("maybe");
    expect(decision.status).toBe("open");
    expect(decision.available_actions).toEqual([]);
    expect(decision.recommended.missing).toBe(true);
    expect(Object.keys(decision)).not.toContain("owner_employee_id");
    expect(Object.keys(decision)).not.toContain("owner");
  });

  it("fails an absent/unrecognized risk_class closed to the most restrictive tier", () => {
    expect(normalizeDecision({ id: "d" }).risk_class).toBe("irreversible");
    expect(normalizeDecision({ id: "d", risk_class: "bogus" }).risk_class).toBe("irreversible");
    expect(normalizeDecision({ id: "d", risk_class: "read_only" }).risk_class).toBe("read_only");
  });

  it("coerces an unknown classification/status to safe fallbacks and filters bad actions", () => {
    const decision = normalizeDecision({
      id: "d",
      classification: "screaming",
      status: "suppressed",
      available_actions: ["edit", { action: "delegate", label: "Delegate" }, "nuke", 42],
    });
    expect(decision.classification).toBe("maybe");
    expect(decision.status).toBe("open"); // 'suppressed' is never a serialized owner-list status
    expect(decision.available_actions.map((a) => a.action)).toEqual(["edit", "delegate"]);
  });

  it("normalizes nested draft, assignees, and snooze presets", () => {
    const decision = normalizeDecision({
      id: "d",
      draft: { to: "a@b.com", subject: "Re: x", body: "hi", sha256: "abc" },
      assignees: [{ employee_id: "emp_bob", name: "Bob" }, { name: "no id — dropped" }],
      suggested_snoozes: [{ label: "Tomorrow", until: "2026-08-14T09:00:00Z", past_deadline: true }, { label: "no until" }],
    });
    expect(decision.draft?.sha256).toBe("abc");
    expect(decision.assignees).toEqual([{ employee_id: "emp_bob", name: "Bob" }]);
    expect(decision.suggested_snoozes).toHaveLength(1);
    expect(decision.suggested_snoozes[0]!.past_deadline).toBe(true);
  });
});

function decision(overrides: Partial<Decision>): Decision {
  return normalizeDecision({ id: "d", ...overrides });
}

describe("groupDecisions (badge excludes maybe + snoozed)", () => {
  it("routes by classification and pulls snoozed rows out regardless of class", () => {
    const groups = groupDecisions([
      decision({ id: "u", classification: "urgent" }),
      decision({ id: "n", classification: "needs_owner" }),
      decision({ id: "m", classification: "maybe" }),
      decision({ id: "s", classification: "urgent", status: "snoozed" }),
    ]);
    expect(groups.urgent.map((d) => d.id)).toEqual(["u"]);
    expect(groups.needsOwner.map((d) => d.id)).toEqual(["n"]);
    expect(groups.maybe.map((d) => d.id)).toEqual(["m"]);
    expect(groups.snoozed.map((d) => d.id)).toEqual(["s"]);
  });
});
