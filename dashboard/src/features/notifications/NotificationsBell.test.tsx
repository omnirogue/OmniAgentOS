import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../../design";
import type { NotificationRow } from "./types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const useNotificationsMock = vi.fn();
vi.mock("./hooks", () => ({
  useNotifications: () => useNotificationsMock(),
}));

import { NotificationsBell } from "./NotificationsBell";

function unreadRow(id = "n1"): NotificationRow {
  return {
    id,
    kind: "info",
    title: `Notification ${id}`,
    body: "",
    severity: "info",
    ref_type: null,
    ref_id: null,
    created_at: "2026-08-01T00:00:00Z",
    read_at: null,
    acted_at: null,
    read: false,
    acted: false,
    payload: {},
    target: { type: null, id: null, resolved: false, actionable: false, state: null },
  };
}

function hookValue(overrides: Partial<ReturnType<typeof useNotificationsMock>> = {}) {
  return {
    notifications: [unreadRow()],
    unreadCount: 1,
    loading: false,
    error: null,
    refresh: vi.fn(),
    markRead: vi.fn(),
    markAllRead: vi.fn().mockResolvedValue({ ok: true }),
    decideApproval: vi.fn(),
    ...overrides,
  };
}

function renderBell() {
  return render(
    <ToastProvider>
      <NotificationsBell />
    </ToastProvider>,
  );
}

describe("NotificationsBell", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows the hook's (actionable-scoped) unread count as the badge", () => {
    useNotificationsMock.mockReturnValue(hookValue({ unreadCount: 2 }));
    renderBell();
    expect(screen.getByLabelText("2 unread notifications")).toBeInTheDocument();
  });

  it("shows no badge and a neutral label when nothing is unread", () => {
    useNotificationsMock.mockReturnValue(hookValue({ notifications: [], unreadCount: 0 }));
    renderBell();
    expect(screen.getByLabelText("Notifications")).toBeInTheDocument();
  });

  it("calls the bulk mark-all-read action and shows no toast on success", async () => {
    const markAllRead = vi.fn().mockResolvedValue({ ok: true });
    useNotificationsMock.mockReturnValue(hookValue({ markAllRead }));
    renderBell();

    fireEvent.click(screen.getByLabelText("1 unread notifications"));
    fireEvent.click(screen.getByText("Mark all read"));

    await waitFor(() => expect(markAllRead).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/not available/i)).not.toBeInTheDocument();
  });

  it("surfaces a toast (not a crash) when the bulk route 404s", async () => {
    const markAllRead = vi.fn().mockResolvedValue({ ok: false });
    useNotificationsMock.mockReturnValue(hookValue({ markAllRead }));
    renderBell();

    fireEvent.click(screen.getByLabelText("1 unread notifications"));
    fireEvent.click(screen.getByText("Mark all read"));

    await waitFor(() => expect(markAllRead).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/bulk mark-all-read isn't available/i)).toBeInTheDocument();
  });

  it("disables Mark all read when there is nothing unread", () => {
    useNotificationsMock.mockReturnValue(hookValue({ notifications: [], unreadCount: 0 }));
    renderBell();

    fireEvent.click(screen.getByLabelText("Notifications"));
    expect(screen.getByText("Mark all read")).toBeDisabled();
  });
});
