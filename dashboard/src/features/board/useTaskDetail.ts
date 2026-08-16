"use client";

/**
 * useTaskDetail — one data hook for the task "everything" panel (§2.6).
 *
 * Resolves the card with the single-card read (`GET /api/board/{task_id}`),
 * then loads the per-tab sources in parallel: every session that touched the
 * card, the longhaul detail (attempts/workbook/acceptance), the steering
 * conversation, the sub-task children, and the ETA. The drawer and
 * /activity/[taskId] both consume it — one implementation, two mounts.
 *
 * The card's ledger event timeline (`GET /api/ledger/tail?ref=task=<id>`) is
 * LAZY-ON-OPEN (it only ever fetches once a task is actually opened, never
 * once per card on the board list — the per-card GET-storm this pattern
 * exists to avoid) but is fetched by its OWN effect/state, deliberately OUT
 * of the Promise.all above: a 2026-08-04 pre-merge review measured up to
 * ~10s of blanking across Overview/Sessions/Runs when the ledger CLI hit a
 * held flock and rode the shared Promise.all with everything else. The
 * ledger fetch now carries its own ~2s client budget (`withLedgerBudget`) —
 * on timeout or failure it settles to `null` (see `ledger`/`ledgerLoading`
 * below), and the REST of the panel is never held up by it either way.
 *
 * It used to find the card by downloading the WHOLE board (2.66 MB) and then,
 * on a miss, the whole archived feed as well — to render one row. The list scan
 * survives only as the 404 fallback: an older control plane predates the
 * single-card route, and a card that route cannot find may still be sitting in
 * the archived feed.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { collabApi, CollabApiError } from "@/features/collab/client";
import type {
  BoardTaskSessions,
  LedgerEvent,
  LiveBoardTask,
  LonghaulTaskDetail,
  TaskTurn,
} from "@/features/collab/types";
import { fetchBoardChildren, fetchBoardEta, type EtaResponse } from "@/features/chats/chatApi";

/** How long the Ledger tab waits for `GET /api/ledger/tail` before treating
 * it as unavailable. A locked/slow ledger CLI must never hold up this tab
 * (let alone the rest of the panel, which no longer waits on it at all). */
const LEDGER_BUDGET_MS = 2_000;

/** Race `promise` against a `ms`-timeout. The underlying promise is never
 * cancelled (no AbortController plumbed through `collabApi` for this one
 * call) — it is simply ignored past the budget once it settles late, which
 * a generation counter at the call site (see the ledger effect) already
 * guards against turning into a stale state update. */
function withLedgerBudget<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`ledger fetch exceeded its ${ms}ms budget`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (reason: unknown) => {
        clearTimeout(timer);
        reject(reason instanceof Error ? reason : new Error(String(reason)));
      },
    );
  });
}

export interface TaskDetailState {
  task: LiveBoardTask | null;
  sessions: BoardTaskSessions | null;
  longhaul: LonghaulTaskDetail | null;
  conversation: TaskTurn[];
  children: LiveBoardTask[];
  eta: EtaResponse | null;
  /** `null` while loading (see `ledgerLoading`) OR when the ledger CLI did
   * not answer within budget / errored — genuinely distinct from `[]`
   * (loaded, zero events for this card). The Ledger tab renders these two
   * `null`-adjacent cases very differently ("unavailable" vs "no events"),
   * so they must never be collapsed into the same falsy value. */
  ledger: LedgerEvent[] | null;
  /** True from the moment a task opens until its ledger fetch settles
   * (success, budget timeout, or error) — independent of the panel's own
   * `loading`, which never waits on the ledger leg at all. */
  ledgerLoading: boolean;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

/**
 * One card, cheapest first.
 *
 * `GET /api/board/{task_id}` answers with the SAME reconciled shape as a list
 * element, archived cards included. A 404 is ambiguous: "no such card" from a
 * control plane that HAS the route, or "no such route" from an older control
 * plane that predates it — the status cannot tell them apart, and against an
 * older control plane every card 404s, live ones included. So the 404 fallback
 * scans the live board first, then the archived feed. Any other failure
 * (network, 5xx) propagates: silently degrading a wedged backend into "task not
 * found" is a lie the drawer would render.
 */
async function resolveTask(taskId: string): Promise<LiveBoardTask | null> {
  try {
    return await collabApi.boardCard(taskId);
  } catch (reason) {
    if (!(reason instanceof CollabApiError) || reason.status !== 404) throw reason;
  }
  const live = await collabApi.liveBoard().catch((): LiveBoardTask[] => []);
  const fromLive = live.find((candidate) => candidate.id === taskId);
  if (fromLive) return fromLive;
  const archived = await collabApi.liveBoard({ archived: true }).catch((): LiveBoardTask[] => []);
  return archived.find((candidate) => candidate.id === taskId) ?? null;
}

export function useTaskDetail(taskId: string | null): TaskDetailState {
  const [state, setState] = useState<Omit<TaskDetailState, "refresh" | "ledger" | "ledgerLoading">>({
    task: null,
    sessions: null,
    longhaul: null,
    conversation: [],
    children: [],
    eta: null,
    loading: Boolean(taskId),
    error: null,
  });
  const [ledger, setLedger] = useState<LedgerEvent[] | null>(null);
  const [ledgerLoading, setLedgerLoading] = useState(Boolean(taskId));
  const ledgerGeneration = useRef(0);

  const refresh = useCallback(async () => {
    if (!taskId) {
      setState((prev) => ({ ...prev, task: null, loading: false }));
      return;
    }
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const task = await resolveTask(taskId);
      if (!task) throw new Error("This board task was not found.");

      // Per-tab sources: independent failures degrade to empty states, never
      // break the whole panel. The ledger tail is deliberately NOT here —
      // see its own effect below.
      const [sessions, longhaul, conversation, children, eta] = await Promise.all([
        collabApi.fetchTaskSessions(taskId).catch(() => null),
        collabApi.fetchLonghaulDetail(taskId).catch(() => null),
        collabApi.fetchTaskConversation(taskId, 50).catch(() => []),
        (fetchBoardChildren(taskId).catch(() => [])) as Promise<LiveBoardTask[]>,
        fetchBoardEta(taskId).catch(() => null),
      ]);

      setState({
        task,
        sessions,
        longhaul,
        conversation,
        children,
        eta,
        loading: false,
        error: null,
      });
    } catch (reason) {
      setState((prev) => ({
        ...prev,
        loading: false,
        error: reason instanceof Error ? reason.message : "Could not load the task",
      }));
    }
  }, [taskId]);

  useEffect(() => {
    setState((prev) => ({ ...prev, loading: Boolean(taskId) }));
    void refresh();
  }, [refresh, taskId]);

  // The ledger leg: its own effect, its own state, its own budget. Keyed
  // only on `taskId` (never on `refresh`/the rest of the panel's state), so
  // a slow or locked ledger CLI can never delay Overview/Sessions/Runs, and
  // the reverse also holds — this never re-fires just because the operator
  // refreshed the rest of the panel.
  useEffect(() => {
    const request = ++ledgerGeneration.current;
    if (!taskId) {
      setLedger(null);
      setLedgerLoading(false);
      return;
    }
    setLedger(null);
    setLedgerLoading(true);
    withLedgerBudget(collabApi.fetchLedgerTail(taskId, 50), LEDGER_BUDGET_MS)
      .then((rows) => {
        if (request !== ledgerGeneration.current) return;
        setLedger(rows);
        setLedgerLoading(false);
      })
      .catch(() => {
        if (request !== ledgerGeneration.current) return;
        setLedger(null);
        setLedgerLoading(false);
      });
    // Explicit unmount/re-run cleanup: bump the generation so a late-
    // settling fetch (past the 2s budget, or simply slow) can never call
    // setState after this effect instance is gone. The `request !==
    // ledgerGeneration.current` guards above already cover the RE-RUN case
    // (a new taskId bumping the counter before the old promise settles);
    // this closes the UNMOUNT case explicitly rather than relying on React
    // 18's tolerance for a post-unmount setState (a no-op with a dev-mode
    // warning, not a guarantee future React versions keep making).
    //
    // exhaustive-deps' "ref value will have changed by cleanup time"
    // warning is a DOM-node-ref heuristic that does not apply here:
    // ledgerGeneration is a plain mutable counter (never a node), and
    // reading/incrementing whatever its CURRENT value is at cleanup time
    // is exactly the intended behaviour, not a staleness bug (same pattern
    // as features/swarm/hooks.ts's `generation.current`, without an
    // unmount cleanup, since this is the first spot that needed one).
    return () => {
      // eslint-disable-next-line react-hooks/exhaustive-deps
      ledgerGeneration.current++;
    };
  }, [taskId]);

  return { ...state, ledger, ledgerLoading, refresh };
}
