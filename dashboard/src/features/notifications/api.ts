/** Defensive fetch client for the notification feed.
 *
 * List/count/read are un-gated reads that go straight to FastAPI (like the
 * alerts/approvals lists). The actual APPROVE action reuses `api.decideApproval`
 * from lib/api, which posts through the same-origin, token-injecting proxy
 * (SEC-005) — so an approval notification becomes a real approve with the same
 * security path as the Approvals page.
 *
 * Every call goes through `fetchWithTimeout` (hard 10s client timeout), so a
 * wedged backend can never leave the bell spinning forever. */

import { apiUrl } from "../../lib/apiRoute";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import type { NotificationRow } from "./types";

export class NotificationApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "NotificationApiError";
  }
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    // fix7: mark-read is a POST and now token-gated, so it goes same-origin
    // (token proxy); the count/list reads stay direct to FastAPI.
    res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new NotificationApiError("Notifications did not respond in time.", 0);
    }
    const detail = reason instanceof Error ? reason.message : String(reason);
    throw new NotificationApiError(`Could not reach the API: ${detail}`, 0);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.error?.message ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new NotificationApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as T;
}

/** Newest-first notifications; `unreadOnly` restricts to unread rows. */
export function fetchNotifications(unreadOnly = false, limit = 50): Promise<NotificationRow[]> {
  const params = new URLSearchParams();
  if (unreadOnly) params.set("unread", "true");
  params.set("limit", String(limit));
  return fetchJson<NotificationRow[]>(`/api/notifications?${params.toString()}`);
}

/** `actionable` is populated only once the backend adds `?actionable=1` scoping
 * (still-pending-approval-style items). Until that lands, the server ignores the
 * unknown query param and replies with plain `{ unread }` — callers must treat
 * `actionable` as optional and fall back to `unread`, never assume its presence. */
export interface NotificationCountResponse {
  unread: number;
  actionable?: number;
}

export function fetchUnreadCount(): Promise<NotificationCountResponse> {
  return fetchJson<NotificationCountResponse>("/api/notifications/count?actionable=1");
}

export function markNotificationRead(id: string): Promise<NotificationRow> {
  return fetchJson<NotificationRow>(`/api/notifications/${encodeURIComponent(id)}/read`, {
    method: "POST",
    body: "{}",
  });
}

/** Bulk mark-all-read. The backend route (`POST /api/notifications/read-all` in
 * `omniagentos/api/routes/notifications.py`) returns `{ updated: number }` — the
 * count of rows it changed, nothing more. On a dashboard build that hasn't
 * picked up that route yet it 404s, and callers must no-op + surface a toast
 * rather than treat it as a hard failure — see NotificationsBell's
 * `onMarkAllRead`. */
export function markAllNotificationsRead(): Promise<{ updated: number }> {
  return fetchJson<{ updated: number }>("/api/notifications/read-all", {
    method: "POST",
    body: "{}",
  });
}
