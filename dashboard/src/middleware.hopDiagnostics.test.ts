import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";

/**
 * LS-003 / D-1 — a trusted-hop 403 must say WHICH failure it is, in the log.
 *
 * THE DEFECT THIS PINS. Every denial produced one byte-identical response AND
 * no log line at all, so these four situations were indistinguishable:
 *
 *   - the dashboard was never given a secret (nothing could ever succeed —
 *     an estate-wide outage that looks exactly like the guard working);
 *   - the caller bypassed the trusted proxy;
 *   - the proxy is running without its own copy of the secret;
 *   - the proxy and the dashboard hold DIFFERENT secrets (drift — the residual
 *     risk the repair plan names for the D-1 fix itself).
 *
 * That is why LS-003 needed a LiveSim run to find. The 403 RESPONSE stays
 * identical on purpose (a caller outside the proxy learns nothing); only the
 * server-side log distinguishes them.
 *
 * Each test re-imports the module so the denial-log throttle starts empty.
 */

const SECRET = "caddy-injected-secret";

function request(headers?: HeadersInit): NextRequest {
  return new Request("https://dashboard.example.test/api/health", { headers }) as NextRequest;
}

async function freshMiddleware() {
  vi.resetModules();
  return (await import("./middleware")).middleware;
}

describe("trusted-hop denials are diagnosable (LS-003)", () => {
  let warn: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    warn.mockRestore();
    vi.unstubAllEnvs();
    vi.useRealTimers();
  });

  function lastLine(): string {
    expect(warn).toHaveBeenCalled();
    return String(warn.mock.calls[warn.mock.calls.length - 1][0]);
  }

  it("names the DEPLOYMENT GAP when this process has no secret", async () => {
    // The actual LS-003 root cause. Distinguishable from a rejected caller.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "");
    const middleware = await freshMiddleware();

    expect(middleware(request({ "X-Omni-Trusted-Hop": SECRET })).status).toBe(403);
    const line = lastLine();
    expect(line).toContain("reason=hop_secret_unset");
    expect(line).toContain("expected_fp=unset");
    expect(line).toContain("OMNIAGENTOS_TRUSTED_HOP_SECRET is unset");
  });

  it("names a caller that never crossed the proxy", async () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const middleware = await freshMiddleware();

    expect(middleware(request()).status).toBe(403);
    const line = lastLine();
    expect(line).toContain("reason=hop_header_absent");
    expect(line).toContain("supplied_fp=absent");
    expect(line).toContain("/api/health");
  });

  it("names a PROXY that is running without its own copy of the secret", async () => {
    // What `header_up X-Omni-Trusted-Hop {env.UNSET}` produces: the header is
    // injected, but empty. Verified against caddy v2.11.4.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const middleware = await freshMiddleware();

    expect(middleware(request({ "X-Omni-Trusted-Hop": "" })).status).toBe(403);
    const line = lastLine();
    expect(line).toContain("reason=hop_header_empty");
    // "empty" and "absent" are different faults and must not share a word.
    expect(line).toContain("supplied_fp=empty");
  });

  it("names DRIFT-OR-FORGERY, and prints both fingerprints so an operator can tell which", async () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const middleware = await freshMiddleware();

    expect(middleware(request({ "X-Omni-Trusted-Hop": "some-other-secret" })).status).toBe(403);
    const line = lastLine();
    expect(line).toContain("reason=hop_header_mismatch");

    // The two fingerprints are present, 8 hex each, and DIFFERENT — that pair
    // is what turns "403" into "these are two different secrets".
    const expectedFp = /expected_fp=([0-9a-f]{8})/.exec(line);
    const suppliedFp = /supplied_fp=([0-9a-f]{8})/.exec(line);
    expect(expectedFp).not.toBeNull();
    expect(suppliedFp).not.toBeNull();
    expect(expectedFp?.[1]).not.toBe(suppliedFp?.[1]);
  });

  it("NEVER logs the secret itself, or any substring of it", async () => {
    const secret = "0123456789abcdef0123456789abcdef";
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", secret);
    const middleware = await freshMiddleware();

    middleware(request({ "X-Omni-Trusted-Hop": "wrong" }));
    const line = lastLine();
    expect(line).not.toContain(secret);
    // No prefix of the secret leaks either: a fingerprint is a lossy tag, not
    // a truncation. 6 chars is well below the 8-char fingerprint length, so a
    // truncating implementation would fail here.
    expect(line).not.toContain(secret.slice(0, 6));
  });

  it("keeps the 403 RESPONSE identical across every reason", async () => {
    // The whole diagnostic lives server-side. A caller must not be able to
    // probe whether this dashboard has been activated with a secret yet.
    const bodies: string[] = [];
    for (const [secret, header] of [
      ["", SECRET],
      [SECRET, undefined],
      [SECRET, ""],
      [SECRET, "forged"],
    ] as const) {
      vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", secret);
      const middleware = await freshMiddleware();
      const response = middleware(
        request(header === undefined ? undefined : { "X-Omni-Trusted-Hop": header }),
      );
      expect(response.status).toBe(403);
      bodies.push(await response.text());
    }
    expect(new Set(bodies).size).toBe(1);
    expect(bodies[0]).toContain("trusted proxy required");
  });

  it("throttles per FAILURE, and a caller sending random headers cannot amplify it", async () => {
    // The throttle key deliberately excludes the caller-supplied fingerprint:
    // keying on it would let anyone mint unbounded map entries (memory) and one
    // log line per request (amplification).
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const middleware = await freshMiddleware();

    for (let i = 0; i < 200; i += 1) {
      expect(middleware(request({ "X-Omni-Trusted-Hop": `forged-${i}` })).status).toBe(403);
    }
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("still reports a DIFFERENT failure immediately — throttling never hides a new one", async () => {
    // Favourable-absence: a suppressed log must never make a second, distinct
    // fault invisible.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const middleware = await freshMiddleware();

    middleware(request({ "X-Omni-Trusted-Hop": "forged" }));
    expect(warn).toHaveBeenCalledTimes(1);
    middleware(request());
    expect(warn).toHaveBeenCalledTimes(2);
    expect(lastLine()).toContain("reason=hop_header_absent");
  });

  it("fingerprints UTF-8 BYTES, so it agrees with the launcher for a non-ASCII secret", async () => {
    // Golden value shared with tests/scripts/test_trusted_hop_deployment.py and
    // `_hop_fingerprint` in the launcher: FNV-1a/32 over the UTF-8 encoding of
    // "é-secret". Hashing `charCodeAt(i) & 0xff` (UTF-16 low bytes) yields
    // e3ca1f2b instead, which is what made the drift diagnostic report a
    // mismatch for two sides holding the SAME secret.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "\u00e9-secret");
    const middleware = await freshMiddleware();

    expect(middleware(request({ "X-Omni-Trusted-Hop": "forged" })).status).toBe(403);

    const line = lastLine();
    expect(line).toContain("expected_fp=d184d668");
    expect(line).not.toContain("expected_fp=e3ca1f2b");
  });

  it("does not log at all when the request is TRUSTED", async () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const middleware = await freshMiddleware();

    expect(middleware(request({ "X-Omni-Trusted-Hop": SECRET })).status).toBe(200);
    expect(warn).not.toHaveBeenCalled();
  });

  it("the diagnostic does not weaken the guard: production still refuses everything", async () => {
    // The production kill-switch is the invariant the whole fix hangs off.
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    const middleware = await freshMiddleware();

    expect(middleware(request()).status).toBe(403);
    expect(middleware(request({ "X-Omni-Trusted-Hop": "forged" })).status).toBe(403);
    expect(middleware(request({ "X-Omni-Trusted-Hop": "" })).status).toBe(403);
    // …and the one correct value still passes, so this is a guard, not a wall.
    expect(middleware(request({ "X-Omni-Trusted-Hop": SECRET })).status).toBe(200);
  });
});
