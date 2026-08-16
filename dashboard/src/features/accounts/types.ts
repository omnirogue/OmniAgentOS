/**
 * TypeScript mirror of the Accounts API — multiple Claude Code logins the
 * scheduler rotates sessions across to get past any single account's rate
 * limit. Lives in this feature (not lib/contracts.ts, which is FROZEN and
 * owned by the lead — see that file's header comment) — same pattern as
 * features/collab/types.ts and features/routines/types.ts.
 *
 *   GET    /api/accounts            -> { accounts: Account[] }
 *   POST   /api/accounts            -> Account (400 on bad input)
 *   PATCH  /api/accounts/{id}       -> Account
 *   DELETE /api/accounts/{id}       -> { removed: true }
 */

export type AccountAuthType = "config_dir" | "oauth_token" | "api_key";
export type AccountStatus = "ok" | "unknown" | "error" | "rate_limited";

export interface Account {
  id: string;
  label: string;
  auth_type: AccountAuthType;
  config_dir: string | null;
  email: string | null;
  enabled: boolean;
  is_default: boolean;
  status: AccountStatus;
  status_detail: string | null;
  last_used_at: string | null;
  /** Never the secret itself — just whether one is on file. */
  has_secret: boolean;
  /** Operator's self-expiring pause. `paused` is DERIVED server-side from
   * whether `paused_until` is still in the future — don't recompute it here,
   * or the UI can disagree with what rotation actually does. Distinct from
   * `enabled: false` (permanent) and from `status: "rate_limited"` (the
   * provider's own cooldown, which the operator does not own). */
  paused: boolean;
  paused_until: string | null;
  pause_reason: string | null;
  created_at: string;
  updated_at: string;
}

/** POST /api/accounts body. */
export interface AddAccountInput {
  label: string;
  auth_type?: AccountAuthType;
  config_dir?: string;
  secret?: string;
  enabled?: boolean;
}

/** PATCH /api/accounts/{id} body — this page always sends exactly one field
 * per call (a toggle sends `enabled`, "Make default" sends `is_default`). */
export interface UpdateAccountInput {
  enabled?: boolean;
  is_default?: boolean;
}

/** POST /api/accounts/{id}/pause body. Server caps `minutes` at 7 days. */
export interface PauseAccountInput {
  minutes: number;
  reason?: string;
}

/* ---------------------------------------------------------------- usage ---
 * GET /api/accounts/usage -> { usage: ProviderUsage[] }
 *
 * Quota telemetry each CLI caches on disk: how much of the budget is LEFT,
 * as opposed to the reactive cooldown state, which only knows an account has
 * already been refused. Mirrors omniagentos/accounts/usage.py.
 */

export type UsageWindowKind = "session" | "weekly_all" | "weekly_scoped";
export type UsageSeverity = "normal" | "warning" | "critical";

export interface UsageWindow {
  kind: UsageWindowKind;
  /** Pre-built human label, e.g. "Session (5h)" / "Weekly · Fable". */
  label: string;
  /** 0-100 consumed. */
  percent: number;
  severity: UsageSeverity;
  resets_at: string | null;
  window_minutes: number | null;
  scope_model: string | null;
  /** The provider says this is the binding window right now. */
  is_active: boolean;
}

export interface AccountCredits {
  enabled: boolean;
  used: number | null;
  limit: number | null;
  /** Major units (dollars), already divided by `decimal_places`. */
  used_amount: number | null;
  limit_amount: number | null;
  percent: number | null;
  currency: string | null;
  /** `null` = scale unknown (missing/unparseable). Never 0 — 0 is a measured
   * scale that would invent major units via 10**0. Mirrors
   * omniagentos/accounts/usage.py's `AccountCredits.decimal_places`. The
   * backend pins this as JSON null on the wire — present, never 0, never
   * omitted (tests/accounts/test_usage.py::
   * test_unknown_decimal_places_serializes_as_json_null_not_zero). No
   * dashboard code currently reads this field directly — `used_amount` /
   * `limit_amount` arrive already converted to major units, so a null scale
   * never needs to be divided out client-side. */
  decimal_places: number | null;
  balance: string | null;
  unlimited: boolean;
  disabled_reason: string | null;
}

export interface ProviderUsage {
  provider: string;
  /** config_dir for claude — the join key back to `Account.config_dir`. */
  account_key: string | null;
  email: string | null;
  plan: string | null;
  available: boolean;
  /** Always set when `available` is false — surface it rather than a zero. */
  reason: string | null;
  windows: UsageWindow[];
  credits: AccountCredits | null;
  /** When the CLI captured this, NOT when we read it. */
  fetched_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  source: string | null;
}
