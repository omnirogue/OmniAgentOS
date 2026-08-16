import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * LS-003 / D-1 — the serverProxy half of the denial diagnostics.
 *
 * `middleware.ts` and `requireTrustedHop` are two deliberately independent
 * fail-closed checks (middleware must stay free of this module's Node-only
 * imports), so the diagnostic is duplicated too. These tests exist to stop the
 * copies drifting apart: the reason vocabulary and the response opacity must be
 * identical, and only the `layer=` tag may differ.
 *
 * Companion: `middleware.hopDiagnostics.test.ts`.
 */

const SECRET = "caddy-injected-secret";

const { fetchWithTimeoutMock, readFileMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
  readFileMock: vi.fn(),
}));

vi.mock("node:fs/promises", () => ({
  default: { readFile: readFileMock },
  readFile: readFileMock,
}));
vi.mock("./fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeoutMock(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

function request(headers?: HeadersInit): Request {
  return new Request("https://dashboard.example.test/api/access/servers", { headers });
}

async function freshModule() {
  vi.resetModules();
  return await import("./serverProxy");
}

describe("requireTrustedHop denials are diagnosable (LS-003)", () => {
  let warn: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    readFileMock.mockReset();
    readFileMock.mockResolvedValue("disk-only-session-token\n");
    fetchWithTimeoutMock.mockReset();
    fetchWithTimeoutMock.mockResolvedValue(
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  });

  afterEach(() => {
    warn.mockRestore();
    vi.unstubAllEnvs();
  });

  function lastLine(): string {
    expect(warn).toHaveBeenCalled();
    return String(warn.mock.calls[warn.mock.calls.length - 1][0]);
  }

  it.each([
    ["", SECRET, "hop_secret_unset"],
    [SECRET, undefined, "hop_header_absent"],
    [SECRET, "", "hop_header_empty"],
    [SECRET, "some-other-secret", "hop_header_mismatch"],
  ])("secret=%j header=%j names reason %s", async (secret, header, reason) => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", secret as string);
    const { requireTrustedHop } = await freshModule();

    const denied = requireTrustedHop(
      request(header === undefined ? undefined : { "X-Omni-Trusted-Hop": header }),
    );

    expect(denied?.status).toBe(403);
    const line = lastLine();
    expect(line).toContain(`reason=${reason}`);
    expect(line).toContain("layer=serverProxy");
  });

  it("uses the SAME reason vocabulary as the middleware copy", async () => {
    // The two guards are independent implementations on purpose; the words they
    // use to describe a failure must not be.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);

    vi.resetModules();
    const { requireTrustedHop } = await import("./serverProxy");
    requireTrustedHop(request({ "X-Omni-Trusted-Hop": "forged" }));
    const proxyLine = lastLine();

    vi.resetModules();
    const { middleware } = await import("../middleware");
    middleware(request({ "X-Omni-Trusted-Hop": "forged" }) as never);
    const middlewareLine = lastLine();

    const reasonOf = (line: string) => /reason=(\w+)/.exec(line)?.[1];
    const fingerprintsOf = (line: string) =>
      /expected_fp=(\S+) supplied_fp=(\S+)/.exec(line)?.slice(1, 3);

    expect(reasonOf(proxyLine)).toBe(reasonOf(middlewareLine));
    // Same inputs must produce the same fingerprints in both runtimes, or an
    // operator comparing a middleware line with a serverProxy line would see a
    // difference that is not there.
    expect(fingerprintsOf(proxyLine)).toEqual(fingerprintsOf(middlewareLine));
    expect(proxyLine).toContain("layer=serverProxy");
    expect(middlewareLine).toContain("layer=middleware");
  });

  it("never logs the secret, and keeps every 403 body identical", async () => {
    const secret = "0123456789abcdef0123456789abcdef";
    const bodies: string[] = [];
    for (const [configured, header] of [
      ["", secret],
      [secret, undefined],
      [secret, ""],
      [secret, "forged"],
    ] as const) {
      vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", configured);
      const { requireTrustedHop } = await freshModule();
      const denied = requireTrustedHop(
        request(header === undefined ? undefined : { "X-Omni-Trusted-Hop": header }),
      );
      expect(denied?.status).toBe(403);
      bodies.push(await denied!.text());
      expect(lastLine()).not.toContain(secret.slice(0, 6));
    }
    expect(new Set(bodies).size).toBe(1);
  });

  it("does not log when the hop is valid", async () => {
    // A no-op bypass no longer exists in this module (see
    // serverProxy.devEscape.test.ts): the local-development mechanism now
    // lives entirely in middleware.ts, which converts a browser credential
    // into a genuine hop assertion before this function ever runs. This guard
    // has exactly two outcomes — valid (silent) or denied (logged) — with no
    // third, unlogged bypass path.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const { requireTrustedHop } = await freshModule();
    expect(requireTrustedHop(request({ "X-Omni-Trusted-Hop": SECRET }))).toBeNull();
    expect(warn).not.toHaveBeenCalled();
  });

  it("PRODUCTION and development are refused identically: every read/mutation stays refused with no bypass", async () => {
    // The kill-switch the brief requires be verifiably intact. Asserted here as
    // well as in serverProxy.devEscape.test.ts because this file is what a
    // future edit to the denial path will be run against.
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const { requireTrustedHop, proxyRead, proxyAuthorized } = await freshModule();

    expect(requireTrustedHop(request())?.status).toBe(403);
    expect(requireTrustedHop(request({ "X-Omni-Trusted-Hop": "forged" }))?.status).toBe(403);

    expect((await proxyRead("/api/access/servers", request())).status).toBe(403);
    const mutation = new Request("https://dashboard.example.test/api/tasks", {
      method: "POST",
      headers: { "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json" },
      body: "{}",
    });
    expect((await proxyAuthorized("/api/tasks", mutation, "POST")).status).toBe(403);
    // The session token is never read on a refused path.
    expect(readFileMock).not.toHaveBeenCalled();
  });

  it("fingerprints UTF-8 BYTES, so it agrees with the launcher for a non-ASCII secret", async () => {
    // Golden value shared with tests/scripts/test_trusted_hop_deployment.py and
    // `_hop_fingerprint` in the launcher: FNV-1a/32 over the UTF-8 encoding of
    // "é-secret". Hashing `charCodeAt(i) & 0xff` (UTF-16 low bytes) yields
    // e3ca1f2b instead, which is what made the drift diagnostic report a
    // mismatch for two sides holding the SAME secret.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "\u00e9-secret");
    const { requireTrustedHop } = await freshModule();

    expect(requireTrustedHop(request({ "X-Omni-Trusted-Hop": "forged" }))?.status).toBe(403);

    const line = lastLine();
    expect(line).toContain("expected_fp=d184d668");
    expect(line).not.toContain("expected_fp=e3ca1f2b");
  });

  it("a random-header flood cannot amplify the log", async () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", SECRET);
    const { requireTrustedHop } = await freshModule();

    for (let i = 0; i < 200; i += 1) {
      expect(requireTrustedHop(request({ "X-Omni-Trusted-Hop": `forged-${i}` }))?.status).toBe(403);
    }
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
