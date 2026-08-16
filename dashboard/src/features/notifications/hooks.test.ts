import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NotificationRow } from "./types";

const fetchNotificationsMock = vi.fn();
const fetchUnreadCountMock = vi.fn();
const markAllNotificationsReadMock = vi.fn();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    fetchNotifications: (...args: Parameters<typeof actual.fetchNotifications>) =>
      fetchNotificationsMock(...args),
    fetchUnreadCount: () => fetchUnreadCountMock(),
    markAllNotificationsRead: () => markAllNotificationsReadMock(),
  };
});

// useNotifications only reads `lastEvent`; stub the shared-stream hook so the
// test never needs a live <EventStreamProvider>/EventSource.
vi.mock("../../lib/useEventChannel", () => ({
  useEventChannel: () => ({ lastEvent: null, events: [], connected: true, error: false }),
}));

import { NotificationApiError } from "./api";
import { useNotifications } from "./hooks";

function row(id: string, read: boolean): NotificationRow {
  return {
    id,
    kind: "info",
    title: `Notification ${id}`,
    body: "",
    severity: "info",
    ref_type: null,
    ref_id: null,
    created_at: "2026-08-01T00:00:00Z",
    read_at: read ? "2026-08-01T00:00:00Z" : null,
    acted_at: null,
    read,
    acted: false,
    payload: {},
    target: { type: null, id: null, resolved: false, actionable: false, state: null },
  };
}

describe("useNotifications — actionable-scoped badge count", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("falls back to the plain unread total pre-backend-commit (no `actionable` field)", async () => {
    fetchNotificationsMock.mockResolvedValue([row("1", false), row("2", false)]);
    fetchUnreadCountMock.mockResolvedValue({ unread: 2 });

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.unreadCount).toBe(2);
  });

  it("scopes the badge to `actionable` once the backend returns it", async () => {
    fetchNotificationsMock.mockResolvedValue([row("1", false), row("2", false), row("3", false)]);
    fetchUnreadCountMock.mockResolvedValue({ unread: 3, actionable: 1 });

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.unreadCount).toBe(1);
  });

  it("falls back to the locally derived total when the count endpoint itself fails", async () => {
    fetchNotificationsMock.mockResolvedValue([row("1", false)]);
    fetchUnreadCountMock.mockRejectedValue(new NotificationApiError("boom", 0));

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.unreadCount).toBe(1);
  });

  it("renders an actionable count of 0 as 0, never falling back to `unread` (nullish coalescing, not ||)", async () => {
    fetchNotificationsMock.mockResolvedValue([
      row("1", false),
      row("2", false),
      row("3", false),
      row("4", false),
      row("5", false),
    ]);
    fetchUnreadCountMock.mockResolvedValue({ unread: 5, actionable: 0 });

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.unreadCount).toBe(0);
  });
});

describe("useNotifications — bulk markAllRead", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("marks everything read and refreshes the feed on success", async () => {
    fetchNotificationsMock.mockResolvedValue([]);
    fetchUnreadCountMock.mockResolvedValue({ unread: 0 });
    // Real server shape (omniagentos/api/routes/notifications.py) is `{ updated }`.
    markAllNotificationsReadMock.mockResolvedValue({ updated: 3 });

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchNotificationsMock).toHaveBeenCalledTimes(1);

    let outcome: { ok: boolean } | undefined;
    await act(async () => {
      outcome = await result.current.markAllRead();
    });

    expect(outcome).toEqual({ ok: true });
    expect(markAllNotificationsReadMock).toHaveBeenCalledTimes(1);
    // A successful bulk mark-read triggers a follow-up refresh of the list.
    expect(fetchNotificationsMock).toHaveBeenCalledTimes(2);
  });

  it("resolves { ok: false } instead of throwing when the route 404s (not shipped yet)", async () => {
    fetchNotificationsMock.mockResolvedValue([]);
    fetchUnreadCountMock.mockResolvedValue({ unread: 0 });
    markAllNotificationsReadMock.mockRejectedValue(new NotificationApiError("not found", 404));

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));

    let outcome: { ok: boolean } | undefined;
    await act(async () => {
      outcome = await result.current.markAllRead();
    });

    expect(outcome).toEqual({ ok: false });
  });

  it("re-throws a non-404 failure so the caller can surface a real error", async () => {
    fetchNotificationsMock.mockResolvedValue([]);
    fetchUnreadCountMock.mockResolvedValue({ unread: 0 });
    markAllNotificationsReadMock.mockRejectedValue(new NotificationApiError("server error", 500));

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await expect(result.current.markAllRead()).rejects.toBeInstanceOf(NotificationApiError);
  });
});
