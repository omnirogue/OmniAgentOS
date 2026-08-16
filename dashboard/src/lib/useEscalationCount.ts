"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Approval } from "./contracts";

/**
 * Is this pending approval invisible to `sessions_awaiting_approval` below?
 *
 * That number is `COUNT(*) FROM sessions WHERE state = 'awaiting_approval'`
 * (`omniagentos/api/routes/today.py`), so it represents an approval ONLY through
 * the session row the approval is attached to. An approval with no `session_id`
 * has no such row and is therefore counted here — and, symmetrically, one WITH a
 * `session_id` is not, because the sessions count already speaks for it. Keying
 * on `session_id` rather than on the payload is what makes the two halves
 * partition the pending set instead of overlapping or leaving a hole.
 *
 * The R8 loop-visibility brief found the hole via `risk === "loop_approval"`
 * (`omniagentos_loops/approvals.py::LOOP_APPROVAL_KIND`) after a loop approval
 * sat 57 minutes unseen. That marker was too narrow by exactly one producer:
 * `omniagentos/runner/core.py` parks a RUN awaiting a human with
 * `risk = str(params.get("risk", ""))` — a step risk word, or "" — and never
 * writes a `session_id` or touches the `sessions` table, so a run parked on an
 * `irreversible` step counted zero in BOTH halves and the badge read 0
 * indefinitely. Both producers share one property: no session row. See #36.
 */
function hasNoParkedSession(approval: Approval): boolean {
  return !approval.session_id;
}

export function useEscalationCount() {
  const [count, setCount] = useState(0);

  const refresh = useCallback(async () => {
    // Two independent reads, each degrading to 0 on its own failure rather
    // than one failure zeroing out both signals.
    const [todayResult, approvalsResult] = await Promise.allSettled([
      // Count sessions actually parked awaiting approval -- NOT the length of
      // the escalations array (that query is LIMIT 20, so it could never show
      // more than 20) and NOT the unread-notification backlog (rows can
      // outlive the session they refer to). The nav badge means "this many
      // things need you right now".
      api.get<{ sessions_awaiting_approval?: number }>("/api/dashboard/today"),
      // Pending approvals no session row can account for: same existing
      // endpoint /approvals already uses, no response-shape change. This is
      // the other half of the gap above -- a loop OR a run parking an approval
      // moves this badge too.
      api.get<Approval[]>("/api/approvals?state=pending"),
    ]);

    const awaiting =
      todayResult.status === "fulfilled" &&
      typeof todayResult.value.sessions_awaiting_approval === "number"
        ? todayResult.value.sessions_awaiting_approval
        : 0;
    const pendingSessionlessApprovals =
      approvalsResult.status === "fulfilled"
        ? approvalsResult.value.filter(hasNoParkedSession).length
        : 0;

    setCount(awaiting + pendingSessionlessApprovals);
  }, []);

  useEffect(() => {
    void refresh();
    // Refresh every 30 seconds
    const interval = setInterval(() => void refresh(), 30000);
    return () => clearInterval(interval);
  }, [refresh]);

  return { count };
}
