import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchNotifications,
  fetchUnreadCount,
  markAllNotificationsRead,
  markNotificationRead,
  NotificationApiError,
} from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    json: async () => body,
  } as Response;
}

describe("notifications api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requests newest-first list with an unread filter", async () => {
    const fetchMock = vi.fn(async () => jsonResponse([{ id: "ntf_1" }]));
    vi.stubGlobal("fetch", fetchMock);

    const rows = await fetchNotifications(true, 25);
    expect(rows).toEqual([{ id: "ntf_1" }]);
    const url = (fetchMock.mock.calls[0] as unknown as [string])[0];
    expect(url).toContain("/api/notifications?");
    expect(url).toContain("unread=true");
    expect(url).toContain("limit=25");
  });

  it("reads the unread count scoped to actionable=1", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ unread: 3 }));
    vi.stubGlobal("fetch", fetchMock);

    // Pre-backend-commit: the server ignores the unknown query param and
    // replies with the plain total — no `actionable` field.
    expect(await fetchUnreadCount()).toEqual({ unread: 3 });
    const url = (fetchMock.mock.calls[0] as unknown as [string])[0];
    expect(url).toContain("/api/notifications/count");
    expect(url).toContain("actionable=1");
  });

  it("reads an actionable-scoped count once the backend adds the field", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ unread: 5, actionable: 2 })));
    expect(await fetchUnreadCount()).toEqual({ unread: 5, actionable: 2 });
  });

  it("posts a mark-read", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ id: "ntf_1", read: true }));
    vi.stubGlobal("fetch", fetchMock);

    const row = await markNotificationRead("ntf_1");
    expect(row).toMatchObject({ id: "ntf_1", read: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/notifications/ntf_1/read");
    expect(init.method).toBe("POST");
  });

  it("throws a typed error on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: { message: "boom" } }, 500)),
    );
    await expect(fetchNotifications()).rejects.toBeInstanceOf(NotificationApiError);
  });

  it("posts a bulk mark-all-read", async () => {
    // Real server shape (omniagentos/api/routes/notifications.py) is
    // `{ updated: <count> }` — not `{ ok, count }`.
    const fetchMock = vi.fn(async () => jsonResponse({ updated: 4 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await markAllNotificationsRead();
    expect(result).toEqual({ updated: 4 });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/notifications/read-all");
    expect(init.method).toBe("POST");
  });

  it("surfaces a 404 from mark-all-read as a typed error (route not shipped yet)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({}, 404)));
    const rejection = markAllNotificationsRead();
    await expect(rejection).rejects.toBeInstanceOf(NotificationApiError);
    await expect(rejection).rejects.toMatchObject({ status: 404 });
  });
});
