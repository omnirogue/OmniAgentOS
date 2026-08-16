import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Approval } from "./contracts";

const apiGet = vi.fn();

vi.mock("./api", () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}));

import { useEscalationCount } from "./useEscalationCount";

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "apr_1",
    run_id: null,
    task_id: null,
    step_seq: null,
    action_class: "consequential",
    proposed_action: "w3_health_monitor/diagnose: restart service",
    risk: "loop_approval",
    evidence: "",
    state: "pending",
    decided_by: null,
    decision_note: null,
    decided_at: null,
    expires_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function mock(today: Record<string, unknown>, approvals: Approval[]) {
  apiGet.mockImplementation((path: string) => {
    if (path === "/api/dashboard/today") return Promise.resolve(today);
    if (path.startsWith("/api/approvals")) return Promise.resolve(approvals);
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

describe("useEscalationCount", () => {
  beforeEach(() => {
    apiGet.mockReset();
  });

  it("adds pending loop approvals to sessions_awaiting_approval (R8: loop approvals carry no session_id)", async () => {
    mock(
      { sessions_awaiting_approval: 1 },
      [approval({ id: "apr_a" }), approval({ id: "apr_b" })],
    );

    const { result } = renderHook(() => useEscalationCount());

    await waitFor(() => expect(result.current.count).toBe(3));
  });

  // Was "ignores non-loop pending approvals" (asserting count 1 here), which
  // pinned the defect: a RUN parked awaiting a human carries a step risk word
  // (or ""), never "loop_approval", and moves no `sessions` row, so it counted
  // zero in BOTH halves of the badge. The rule is session_id, not risk.
  it("counts a run-scoped pending approval the sessions count cannot see", async () => {
    mock(
      { sessions_awaiting_approval: 1 },
      [
        approval({
          id: "apr_a",
          risk: "", // runner/core.py: str(params.get("risk", ""))
          run_id: "run_1",
          task_id: "tsk_1",
          step_seq: 3,
          action_class: "irreversible",
        }),
      ],
    );

    const { result } = renderHook(() => useEscalationCount());

    await waitFor(() => expect(result.current.count).toBe(2));
  });

  it("does not double-count an approval already represented by its parked session", async () => {
    mock(
      { sessions_awaiting_approval: 1 },
      [approval({ id: "apr_a", risk: "", session_id: "ses_1" })],
    );

    const { result } = renderHook(() => useEscalationCount());

    await waitFor(() => expect(result.current.count).toBe(1));
  });

  it("degrades to the today-only count when the approvals fetch fails, rather than zeroing the badge", async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === "/api/dashboard/today") return Promise.resolve({ sessions_awaiting_approval: 2 });
      if (path.startsWith("/api/approvals")) return Promise.reject(new Error("503"));
      return Promise.reject(new Error(`unexpected path ${path}`));
    });

    const { result } = renderHook(() => useEscalationCount());

    await waitFor(() => expect(result.current.count).toBe(2));
  });

  it("degrades to the loop-approval-only count when the today fetch fails", async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === "/api/dashboard/today") return Promise.reject(new Error("503"));
      if (path.startsWith("/api/approvals")) return Promise.resolve([approval()]);
      return Promise.reject(new Error(`unexpected path ${path}`));
    });

    const { result } = renderHook(() => useEscalationCount());

    await waitFor(() => expect(result.current.count).toBe(1));
  });

  it("is zero, not a crash, when both fetches fail", async () => {
    apiGet.mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useEscalationCount());

    await waitFor(() => expect(result.current.count).toBe(0));
  });
});
