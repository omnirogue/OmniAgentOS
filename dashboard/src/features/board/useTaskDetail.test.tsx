import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveBoardTask } from "@/features/collab/types";

/**
 * The detail panel used to find its card by downloading the WHOLE board, and
 * then the whole archived feed on a miss — megabytes to render one row. These
 * pin the replacement: one single-card read, and the list scan surviving ONLY
 * as the 404 fallback.
 */

const mocks = vi.hoisted(() => {
  class TestCollabApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "CollabApiError";
      this.status = status;
    }
  }
  return {
  TestCollabApiError,
  boardCard: vi.fn(),
  liveBoard: vi.fn(),
  fetchTaskSessions: vi.fn(),
  fetchLonghaulDetail: vi.fn(),
  fetchTaskConversation: vi.fn(),
  fetchBoardChildren: vi.fn(),
  fetchBoardEta: vi.fn(),
  fetchLedgerTail: vi.fn(),
  };
});

const TestCollabApiError = mocks.TestCollabApiError;

vi.mock("@/features/collab/client", () => ({
  CollabApiError: mocks.TestCollabApiError,
  collabApi: {
    boardCard: mocks.boardCard,
    liveBoard: mocks.liveBoard,
    fetchTaskSessions: mocks.fetchTaskSessions,
    fetchLonghaulDetail: mocks.fetchLonghaulDetail,
    fetchTaskConversation: mocks.fetchTaskConversation,
    fetchLedgerTail: mocks.fetchLedgerTail,
  },
}));

vi.mock("@/features/chats/chatApi", () => ({
  fetchBoardChildren: mocks.fetchBoardChildren,
  fetchBoardEta: mocks.fetchBoardEta,
}));

import { useTaskDetail } from "./useTaskDetail";

function card(id: string): LiveBoardTask {
  return {
    id,
    title: `Card ${id}`,
    description: "",
    required_expertise: [],
    discipline: null,
    priority: "normal",
    status: "open",
    claimed_by: null,
    claim_version: 0,
    result_ref: null,
    created_at: "2026-08-01T10:00:00.000Z",
    updated_at: "2026-08-01T10:00:00.000Z",
    run_id: null,
    run_state: null,
    run_agent: null,
    run_progress: null,
    run_error: null,
    paused_run: null,
    paused_session: null,
  };
}

describe("useTaskDetail card resolution", () => {
  beforeEach(() => {
    for (const [key, mock] of Object.entries(mocks)) {
      if (key !== "TestCollabApiError") (mock as ReturnType<typeof vi.fn>).mockReset();
    }
    mocks.fetchTaskSessions.mockResolvedValue(null);
    mocks.fetchLonghaulDetail.mockResolvedValue(null);
    mocks.fetchTaskConversation.mockResolvedValue([]);
    mocks.fetchBoardChildren.mockResolvedValue([]);
    mocks.fetchBoardEta.mockResolvedValue(null);
    mocks.fetchLedgerTail.mockResolvedValue([]);
    mocks.liveBoard.mockResolvedValue([]);
  });

  it("reads the one card, never the whole board", async () => {
    mocks.boardCard.mockResolvedValue(card("task-1"));

    const { result } = renderHook(() => useTaskDetail("task-1"));

    await waitFor(() => expect(result.current.task?.id).toBe("task-1"));
    expect(mocks.boardCard).toHaveBeenCalledWith("task-1");
    expect(mocks.liveBoard).not.toHaveBeenCalled();
  });

  it("falls back to the live board, then the archived feed, on a 404", async () => {
    mocks.boardCard.mockRejectedValue(new TestCollabApiError("404: not found", 404));
    mocks.liveBoard.mockImplementation((params?: { archived?: boolean }) =>
      Promise.resolve(params?.archived ? [card("task-2")] : []),
    );

    const { result } = renderHook(() => useTaskDetail("task-2"));

    await waitFor(() => expect(result.current.task?.id).toBe("task-2"));
    expect(mocks.liveBoard).toHaveBeenCalledWith();
    expect(mocks.liveBoard).toHaveBeenCalledWith({ archived: true });
  });

  it("resolves a LIVE card through the 404 fallback (older control plane without the card route)", async () => {
    // Against a control plane that predates GET /api/board/{id}, EVERY card
    // 404s — live ones included. The archived-only fallback rendered those as
    // "not found"; the live scan must catch them first.
    mocks.boardCard.mockRejectedValue(new TestCollabApiError("404: not found", 404));
    mocks.liveBoard.mockImplementation((params?: { archived?: boolean }) =>
      Promise.resolve(params?.archived ? [] : [card("task-5")]),
    );

    const { result } = renderHook(() => useTaskDetail("task-5"));

    await waitFor(() => expect(result.current.task?.id).toBe("task-5"));
    expect(mocks.liveBoard).not.toHaveBeenCalledWith({ archived: true });
  });

  it("reports not-found when neither the card route nor the archive has it", async () => {
    mocks.boardCard.mockRejectedValue(new TestCollabApiError("404: not found", 404));
    mocks.liveBoard.mockResolvedValue([]);

    const { result } = renderHook(() => useTaskDetail("ghost"));

    await waitFor(() => expect(result.current.error).toBe("This board task was not found."));
  });

  it("surfaces a wedged backend instead of calling the card missing", async () => {
    // A 500 is not "no such task"; degrading it into one is a lie the drawer
    // would render, and it would also trigger a pointless archived-feed download.
    mocks.boardCard.mockRejectedValue(new TestCollabApiError("500: boom", 500));

    const { result } = renderHook(() => useTaskDetail("task-3"));

    await waitFor(() => expect(result.current.error).toBe("500: boom"));
    expect(mocks.liveBoard).not.toHaveBeenCalled();
  });
});

describe("useTaskDetail ledger timeline (own effect/state, budgeted, decoupled from the rest)", () => {
  beforeEach(() => {
    for (const [key, mock] of Object.entries(mocks)) {
      if (key !== "TestCollabApiError") (mock as ReturnType<typeof vi.fn>).mockReset();
    }
    mocks.fetchTaskSessions.mockResolvedValue(null);
    mocks.fetchLonghaulDetail.mockResolvedValue(null);
    mocks.fetchTaskConversation.mockResolvedValue([]);
    mocks.fetchBoardChildren.mockResolvedValue([]);
    mocks.fetchBoardEta.mockResolvedValue(null);
    mocks.liveBoard.mockResolvedValue([]);
    mocks.boardCard.mockResolvedValue(card("task-ledger"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fetches the ledger tail for this card once it is opened, n=50", async () => {
    mocks.fetchLedgerTail.mockResolvedValue([]);

    const { result } = renderHook(() => useTaskDetail("task-ledger"));

    await waitFor(() => expect(result.current.ledgerLoading).toBe(false));
    expect(mocks.fetchLedgerTail).toHaveBeenCalledWith("task-ledger", 50);
    expect(mocks.fetchLedgerTail).toHaveBeenCalledTimes(1);
    expect(result.current.ledger).toEqual([]);
  });

  it("exposes null (never []) on fetch failure -- genuinely distinct from loaded-empty", async () => {
    mocks.fetchLedgerTail.mockRejectedValue(new Error("ledger_unavailable"));

    const { result } = renderHook(() => useTaskDetail("task-ledger"));

    await waitFor(() => expect(result.current.ledgerLoading).toBe(false));
    // A ledger failure degrades independently -- it never blanks the rest
    // of the drawer (§2.8), and never surfaces as the panel-level error.
    expect(result.current.ledger).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.task?.id).toBe("task-ledger");
  });

  it("never fetches a ledger tail when no task is open", () => {
    renderHook(() => useTaskDetail(null));

    expect(mocks.fetchLedgerTail).not.toHaveBeenCalled();
    // No open task: nothing to load, so ledgerLoading settles to false too,
    // never stuck true forever.
  });

  it("a never-resolving ledger fetch never delays the rest of the panel (loading:false)", async () => {
    // A pre-merge review measured up to ~10s of blanking across Overview/
    // Sessions/Runs when the ledger leg rode the SAME Promise.all as
    // everything else. It is now its own effect entirely: the main
    // `loading` must settle promptly no matter what the ledger fetch does.
    mocks.fetchLedgerTail.mockReturnValue(new Promise<never>(() => {}));

    const { result } = renderHook(() => useTaskDetail("task-ledger"));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.task?.id).toBe("task-ledger");
    // The ledger leg is still (legitimately) in flight -- unaffected.
    expect(result.current.ledgerLoading).toBe(true);
  });

  it("times out after its own ~2s budget and settles to unavailable (null), not stuck loading forever", async () => {
    vi.useFakeTimers();
    mocks.fetchLedgerTail.mockReturnValue(new Promise<never>(() => {}));

    const { result } = renderHook(() => useTaskDetail("task-ledger"));
    expect(result.current.ledgerLoading).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_999);
    });
    expect(result.current.ledgerLoading).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.ledgerLoading).toBe(false);
    expect(result.current.ledger).toBeNull();
  });

  it("re-fetches its own leg when the taskId changes, independent of refresh()", async () => {
    mocks.fetchLedgerTail.mockResolvedValue([]);

    const { result, rerender } = renderHook(({ id }: { id: string }) => useTaskDetail(id), {
      initialProps: { id: "task-ledger" },
    });
    // Wait on the settled STATE, not merely "the mock was called" -- being
    // called and the resulting promise having actually resolved (and its
    // state update flushed) are two different ticks, and asserting only the
    // former is a race under load (flaky in the full suite, not in
    // isolation).
    await waitFor(() => expect(result.current.ledgerLoading).toBe(false));
    expect(mocks.fetchLedgerTail).toHaveBeenCalledWith("task-ledger", 50);

    mocks.boardCard.mockResolvedValue(card("task-other"));
    mocks.fetchLedgerTail.mockClear();
    rerender({ id: "task-other" });

    await waitFor(() => expect(result.current.ledgerLoading).toBe(false));
    expect(mocks.fetchLedgerTail).toHaveBeenCalledWith("task-other", 50);
  });

  it("never updates state after unmount when the ledger fetch settles late (explicit cleanup, not React tolerance)", async () => {
    let resolveFetch: (rows: unknown[]) => void = () => {};
    mocks.fetchLedgerTail.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = renderHook(() => useTaskDetail("task-ledger"));
    unmount();

    // Settle the fetch AFTER teardown -- the effect's cleanup must already
    // have bumped the generation counter, so the .then() handler's guard
    // (`request !== ledgerGeneration.current`) short-circuits before ever
    // calling setState on an unmounted component.
    await act(async () => {
      resolveFetch([]);
      await Promise.resolve();
      await Promise.resolve();
    });

    const unmountedWarning = errorSpy.mock.calls.some((call) =>
      call.some((arg) => typeof arg === "string" && arg.includes("unmounted")),
    );
    expect(unmountedWarning).toBe(false);
    errorSpy.mockRestore();
  });
});
