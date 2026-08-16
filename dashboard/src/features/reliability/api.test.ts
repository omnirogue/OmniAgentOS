import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchWithTimeout = vi.fn();

vi.mock("../../lib/fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeout(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

import {
  decideImprovement,
  fetchEventHubStatus,
  fetchHealthSummary,
  normalizeEventHubStatus,
  normalizeHealthSummary,
  ReliabilityApiError,
} from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const REPO_FIXTURES = path.resolve(__dirname, "../../../../contracts/fixtures");

/** Dashboard-owned byte-identical copies for offline adapter proof. */
const LOCAL_L03_FIXTURE_PATH = path.resolve(
  __dirname,
  "fixtures/reliability-summary.v1.json",
);
const LOCAL_L12_FIXTURE_PATH = path.resolve(
  __dirname,
  "fixtures/backend-realtime-v1.json",
);
const REPO_L03_FIXTURE_PATH = path.join(
  REPO_FIXTURES,
  "reliability-summary.v1.json",
);
const REPO_L12_FIXTURE_PATH = path.join(
  REPO_FIXTURES,
  "backend-realtime-v1.json",
);

describe("reliability API adapter contracts", () => {
  beforeEach(() => {
    fetchWithTimeout.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("feeds exact B02/L03 reliability-summary.v1 fixture through the real adapter (B06)", () => {
    const l03Bytes = readFileSync(REPO_L03_FIXTURE_PATH);
    const localBytes = readFileSync(LOCAL_L03_FIXTURE_PATH);
    expect(
      createHash("sha256").update(localBytes).digest("hex"),
      "dashboard fixture must be byte-identical to L03 B02 fixture",
    ).toBe(createHash("sha256").update(l03Bytes).digest("hex"));
    expect(Buffer.compare(l03Bytes, localBytes)).toBe(0);

    const raw = JSON.parse(l03Bytes.toString("utf8")) as unknown;
    const summary = normalizeHealthSummary(raw);

    // Null/unavailable counts must not become zeros.
    expect(summary.open_critical).toBeNull();
    expect(summary.open_warning).toBeNull();
    expect(summary.open_info).toBeNull();
    expect(summary.open_events).toEqual({
      info: null,
      warning: null,
      critical: null,
    });
    expect(summary.open_events_state).toBe("unavailable");

    // Separate watch / degraded / incident state preserved.
    expect(summary.health).toBe("degraded");
    expect(summary.degraded_reasons).toEqual([
      "open_events_unavailable",
      "watch_state_unavailable",
    ]);
    expect(summary.watch?.state).toBe("last_known_good");
    expect(summary.watch?.error).toBe("watch_state_unavailable");
    expect(summary.watch?.age_seconds).toBe(60);
    expect(summary.incidents).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "open_events_unavailable" }),
        expect.objectContaining({ code: "watch_state_unavailable" }),
      ]),
    );
    expect(summary.contract_version).toBe("reliability-summary.v1");
    expect(summary.last_audit_state).toBe("current");
  });

  it("keeps the portable L12 fixture byte-identical and validates its cross-contract shape", () => {
    const l12Bytes = readFileSync(REPO_L12_FIXTURE_PATH);
    const localBytes = readFileSync(LOCAL_L12_FIXTURE_PATH);
    expect(
      createHash("sha256").update(localBytes).digest("hex"),
      "dashboard fixture must be byte-identical to the in-repo L12 fixture",
    ).toBe(createHash("sha256").update(l12Bytes).digest("hex"));
    expect(Buffer.compare(l12Bytes, localBytes)).toBe(0);

    const fixture = JSON.parse(l12Bytes.toString("utf8")) as {
      contract_version?: unknown;
      event_hub?: {
        health_field?: unknown;
        sse_event?: unknown;
        degraded_sse_example?: unknown;
      };
      organization?: {
        spawn_failure?: {
          status_code?: unknown;
          error_code?: unknown;
          event_action?: unknown;
          approved_event_emitted?: unknown;
        };
      };
      origins?: { validation?: { allow_wildcard?: unknown } };
    };
    expect(fixture.contract_version).toBe(1);
    expect(fixture.origins?.validation?.allow_wildcard).toBe(false);
    // Restored: this is L12 CONTRACT coverage, not org-client coverage. It was
    // removed with the orphaned client functions, but the contract it guards
    // (503 / spawn_failed / no approved event) is still live and still in both
    // fixtures.
    expect(fixture.organization?.spawn_failure).toMatchObject({
      status_code: 503,
      error_code: "spawn_failed",
      event_action: "agent_request.spawn_failed",
      approved_event_emitted: false,
    });
    expect(fixture.event_hub).toMatchObject({
      health_field: "event_hub",
      sse_event: "eventbus.status",
    });
    expect(fixture.event_hub?.degraded_sse_example).toMatchObject({
      state: "degraded",
      degraded: true,
    });
  });

  it("normalizes health summary with real incident counts and null watch age (H-15)", async () => {
    fetchWithTimeout.mockResolvedValueOnce(
      jsonResponse({
        open_critical: 3,
        open_warning: 7,
        open_info: 1,
        last_audit_status: "completed",
        last_audit_at: "2026-07-25T00:00:00Z",
        watch_cursor_at: null,
        watch_cursor_age_s: null,
        health: "degraded",
      }),
    );

    const summary = await fetchHealthSummary();
    expect(summary.open_critical).toBe(3);
    expect(summary.open_warning).toBe(7);
    expect(summary.watch_cursor_age_s).toBeNull();
    expect(summary.health).toBe("degraded");
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/reliability/summary",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("preserves null counts and does not invent healthy zeros from malformed health", async () => {
    fetchWithTimeout.mockResolvedValueOnce(
      jsonResponse({
        open_critical: "nope",
        health: "mystery",
        watch_cursor_age_s: undefined,
      }),
    );
    const summary = await fetchHealthSummary();
    expect(summary.open_critical).toBeNull();
    expect(summary.open_warning).toBeNull();
    // Fail closed: unknown health is degraded, never invented healthy.
    expect(summary.health).toBe("degraded");
    expect(summary.watch_cursor_age_s).toBeNull();
  });

  it("keeps explicit zero counts when the aggregate is current", () => {
    const summary = normalizeHealthSummary({
      open_critical: 0,
      open_warning: 0,
      open_info: 0,
      open_events_state: "current",
      health: "healthy",
      watch_cursor_age_s: 5,
    });
    expect(summary.open_critical).toBe(0);
    expect(summary.open_warning).toBe(0);
    expect(summary.health).toBe("healthy");
    expect(summary.watch_cursor_age_s).toBe(5);
  });

  it("normalizes L12 health.event_hub degraded status", async () => {
    fetchWithTimeout.mockResolvedValueOnce(
      jsonResponse({
        status: "degraded",
        event_hub: {
          contract_version: 1,
          state: "degraded",
          degraded: true,
          consecutive_failures: 3,
          last_error: "event_sample: RuntimeError: store down",
          tailer_alive: false,
          tailer_restarts: 0,
          max_tailer_restarts: 5,
        },
      }),
    );
    const hub = await fetchEventHubStatus();
    expect(hub).toMatchObject({
      state: "degraded",
      degraded: true,
      consecutive_failures: 3,
      last_error: "event_sample: RuntimeError: store down",
    });
    expect(
      normalizeEventHubStatus({
        status: "ok",
        event_hub: { state: "ok", degraded: false },
      })?.degraded,
    ).toBe(false);
  });

  it("sends decided_by on improvement decide actions including pull", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse({ id: "imp-1", title: "t", status: "awaiting_human" }));
    await decideImprovement("imp-1", "pull", { decided_by: "human" });
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/improvements/imp-1/pull",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decided_by: "human" }),
      }),
    );
  });

  it("refuses approve without explicit decided_by (no silent operator/human default)", async () => {
    await expect(decideImprovement("imp-1", "approve")).rejects.toThrow(/decided_by/i);
    expect(fetchWithTimeout).not.toHaveBeenCalled();
  });

  it("refuses approve when decided_by is empty/whitespace", async () => {
    await expect(
      decideImprovement("imp-1", "approve", { decided_by: "   " }),
    ).rejects.toThrow(/decided_by/i);
    expect(fetchWithTimeout).not.toHaveBeenCalled();
  });

  it("posts the typed decided_by identity on approve (never substitutes operator/human)", async () => {
    fetchWithTimeout.mockResolvedValueOnce(
      jsonResponse({ id: "imp-1", title: "t", status: "awaiting_human", created_by: "agent-x" }),
    );
    await decideImprovement("imp-1", "approve", { decided_by: "owner" });
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/improvements/imp-1/approve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decided_by: "owner" }),
      }),
    );
    const body = JSON.parse(
      (fetchWithTimeout.mock.calls[0]?.[1] as { body?: string })?.body ?? "{}",
    ) as { decided_by?: string };
    expect(body.decided_by).toBe("owner");
    expect(body.decided_by).not.toBe("operator");
    expect(body.decided_by).not.toBe("human");
  });

  it("surfaces reliability API 404 as ReliabilityApiError (H-16)", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse({ error: { message: "not found" } }, 404));
    await expect(fetchHealthSummary()).rejects.toMatchObject({
      name: "ReliabilityApiError",
      status: 404,
      message: "not found",
    });
    expect(ReliabilityApiError).toBeDefined();
  });
});
