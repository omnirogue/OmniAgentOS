import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveBoardTask } from "./types";

/**
 * The board read is CONDITIONAL. These pin the two halves of that contract that
 * a regression would silently break: the tag is replayed, and a 304 keeps the
 * cards on screen instead of blanking the kanban every poll.
 */

const mocks = vi.hoisted(() => ({
  liveBoardConditional: vi.fn(),
  needsResponse: vi.fn(),
}));

vi.mock("./client", () => ({
  liveBoardConditional: mocks.liveBoardConditional,
  collabApi: {
    needsResponse: mocks.needsResponse,
    archiveTask: vi.fn(),
    archiveTasks: vi.fn(),
  },
}));

// No EventSource, no polling: this suite is about the fetch contract.
vi.mock("./fixtures", () => ({ USE_FIXTURES: true, FIXTURE_LIVE_TASKS: [] }));

import { useLiveBoard } from "./hooks";

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

describe("useLiveBoard conditional read", () => {
  beforeEach(() => {
    mocks.liveBoardConditional.mockReset();
    mocks.needsResponse.mockReset();
    mocks.needsResponse.mockResolvedValue({ items: [], count: 0 });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("replays the previous ETag as If-None-Match", async () => {
    mocks.liveBoardConditional.mockResolvedValue({
      tasks: [card("a")],
      etag: 'W/"board-1"',
      notModified: false,
    });

    const { result } = renderHook(() => useLiveBoard());
    await waitFor(() => expect(result.current.hasLoaded).toBe(true));

    expect(mocks.liveBoardConditional).toHaveBeenCalledWith(
      expect.objectContaining({ etag: null }),
    );

    await result.current.refresh();

    expect(mocks.liveBoardConditional).toHaveBeenLastCalledWith(
      expect.objectContaining({ etag: 'W/"board-1"' }),
    );
  });

  it("keeps the cards on a 304 — a 304 is not an empty board", async () => {
    mocks.liveBoardConditional.mockResolvedValueOnce({
      tasks: [card("a"), card("b")],
      etag: 'W/"board-1"',
      notModified: false,
    });
    const { result } = renderHook(() => useLiveBoard());
    await waitFor(() => expect(result.current.tasks).toHaveLength(2));

    mocks.liveBoardConditional.mockResolvedValueOnce({
      tasks: null,
      etag: 'W/"board-1"',
      notModified: true,
    });
    await result.current.refresh();

    await waitFor(() => expect(result.current.tasks).toHaveLength(2));
    expect(result.current.error).toBeNull();
    expect(result.current.hasLoaded).toBe(true);
  });

  it("does not re-fetch the Needs Response band for a 304", async () => {
    mocks.liveBoardConditional.mockResolvedValueOnce({
      tasks: [card("a")],
      etag: 'W/"board-1"',
      notModified: false,
    });
    const { result } = renderHook(() => useLiveBoard());
    await waitFor(() => expect(mocks.needsResponse).toHaveBeenCalledTimes(1));

    mocks.liveBoardConditional.mockResolvedValueOnce({
      tasks: null,
      etag: 'W/"board-1"',
      notModified: true,
    });
    await result.current.refresh();

    expect(mocks.needsResponse).toHaveBeenCalledTimes(1);
  });

  it("exposes the server's Needs Response ids", async () => {
    mocks.liveBoardConditional.mockResolvedValue({
      tasks: [card("a"), card("b")],
      etag: null,
      notModified: false,
    });
    mocks.needsResponse.mockResolvedValue({ items: [card("b")], count: 1 });

    const { result } = renderHook(() => useLiveBoard());

    await waitFor(() => expect([...result.current.needsResponseIds]).toEqual(["b"]));
  });

  it("degrades silently when the band endpoint is unavailable", async () => {
    mocks.liveBoardConditional.mockResolvedValue({
      tasks: [card("a")],
      etag: null,
      notModified: false,
    });
    mocks.needsResponse.mockRejectedValue(new Error("404: not found"));

    const { result } = renderHook(() => useLiveBoard());

    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    expect(result.current.error).toBeNull();
    expect(result.current.needsResponseIds.size).toBe(0);
  });
});
