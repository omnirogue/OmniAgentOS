/**
 * Fetch client for the Accounts API:
 *   GET    /api/accounts
 *   POST   /api/accounts
 *   PATCH  /api/accounts/{id}
 *   DELETE /api/accounts/{id}
 * Same shape as features/routines/api.ts: every call goes same-origin
 * through the Next.js proxy (`apiUrl`), which attaches the local session
 * token to mutations server-side — the browser never holds it.
 */
import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import type {
  Account,
  AddAccountInput,
  PauseAccountInput,
  ProviderUsage,
  UpdateAccountInput,
} from "./types";

export class AccountsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown = null,
  ) {
    super(message);
    this.name = "AccountsApiError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let message = res.statusText;
    let detail: unknown = null;
    try {
      const body = (await res.json()) as { error?: { message?: string; detail?: unknown } };
      message = body?.error?.message ?? message;
      detail = body?.error?.detail ?? null;
    } catch {
      // Non-JSON error body — fall back to statusText.
    }
    throw new AccountsApiError(message, res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function listAccounts(): Promise<Account[]> {
  return req<{ accounts: Account[] }>("/api/accounts").then((body) => body.accounts);
}

export function addAccount(body: AddAccountInput): Promise<Account> {
  return req<Account>("/api/accounts", { method: "POST", body: JSON.stringify(body) });
}

function updateAccount(id: string, body: UpdateAccountInput): Promise<Account> {
  return req<Account>(`/api/accounts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function setAccountEnabled(id: string, enabled: boolean): Promise<Account> {
  return updateAccount(id, { enabled });
}

export function setAccountDefault(id: string): Promise<Account> {
  return updateAccount(id, { is_default: true });
}

export function pauseAccount(id: string, body: PauseAccountInput): Promise<Account> {
  return req<Account>(`/api/accounts/${encodeURIComponent(id)}/pause`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function resumeAccount(id: string): Promise<Account> {
  return req<Account>(`/api/accounts/${encodeURIComponent(id)}/resume`, { method: "POST" });
}

export function listUsage(): Promise<ProviderUsage[]> {
  return req<{ usage: ProviderUsage[] }>("/api/accounts/usage").then((body) => body.usage);
}

export function removeAccount(id: string): Promise<{ removed: boolean }> {
  return req<{ removed: boolean }>(`/api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
}
