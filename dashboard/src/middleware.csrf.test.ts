import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";

/**
 * S-B1 review fix (blocker #2): the CSRF same-origin check exists in TWO
 * independent implementations — `middleware.ts` (Edge-runtime-safe, no
 * Node-only imports) and `serverProxy.ts`'s `proxyAuthorized` (the Node
 * route-handler layer) — because middleware cannot import the Node-only
 * module without breaking the Edge bundle (see middleware.ts's file doc
 * comment). Two independent implementations can silently drift, so this
 * file runs the SAME table of requests against BOTH and asserts they always
 * agree, in addition to covering every mutating verb and the HEAD/OPTIONS
 * non-mutation cases that `middleware.test.ts` (GET-only) does not exercise.
 */

import { requireSameOriginMutation as requireSameOriginMutationMiddleware } from "./middleware";
import { requireSameOriginMutation as requireSameOriginMutationServerProxy } from "./lib/serverProxy";

const MUTATING_METHODS = ["POST", "PUT", "PATCH", "DELETE"] as const;
const NON_MUTATING_METHODS = ["HEAD", "OPTIONS"] as const;
const REMOTE_ORIGIN = "https://mac-studio.tail0000.ts.net";

function request(method: string, headers: HeadersInit = {}): NextRequest {
  return new Request("https://dashboard.example.test/api/tasks", { method, headers }) as NextRequest;
}

type Scenario = {
  name: string;
  headers: HeadersInit;
  allowedOriginsEnv?: string;
  expectAllowed: boolean;
};

const MUTATION_SCENARIOS: Scenario[] = [
  { name: "no Origin, no Sec-Fetch-Site (bare CLI/curl mutation)", headers: {}, expectAllowed: false },
  { name: "Origin: null, no Sec-Fetch-Site (opaque/sandboxed context)", headers: { Origin: "null" }, expectAllowed: false },
  {
    name: 'Origin: null even when "null" is (mis)configured in the allowlist',
    headers: { Origin: "null" },
    allowedOriginsEnv: "null",
    expectAllowed: false,
  },
  { name: "Sec-Fetch-Site: same-site (sibling subdomain)", headers: { "Sec-Fetch-Site": "same-site" }, expectAllowed: false },
  { name: "Sec-Fetch-Site: cross-site", headers: { "Sec-Fetch-Site": "cross-site" }, expectAllowed: false },
  { name: "Sec-Fetch-Site: same-origin", headers: { "Sec-Fetch-Site": "same-origin" }, expectAllowed: true },
  { name: "Sec-Fetch-Site: none (user-typed/bookmark)", headers: { "Sec-Fetch-Site": "none" }, expectAllowed: true },
  {
    name: "allowlisted remote-hostname Origin, no Sec-Fetch-Site",
    headers: { Origin: REMOTE_ORIGIN },
    allowedOriginsEnv: REMOTE_ORIGIN,
    expectAllowed: true,
  },
  {
    name: "unlisted Origin, no Sec-Fetch-Site",
    headers: { Origin: "https://evil.example" },
    allowedOriginsEnv: REMOTE_ORIGIN,
    expectAllowed: false,
  },
  {
    name: "default loopback Origin passes with no configuration at all",
    headers: { Origin: "http://localhost:3003" },
    expectAllowed: true,
  },
];

describe("S-B1 same-origin CSRF check — both layers agree, table-driven", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  for (const method of MUTATING_METHODS) {
    describe(`${method}`, () => {
      for (const scenario of MUTATION_SCENARIOS) {
        it(`${scenario.name} -> ${scenario.expectAllowed ? "allowed" : "denied"}`, () => {
          if (scenario.allowedOriginsEnv) {
            vi.stubEnv("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS", scenario.allowedOriginsEnv);
          }
          const req = request(method, scenario.headers);

          const middlewareVerdict = requireSameOriginMutationMiddleware(req);
          const serverProxyVerdict = requireSameOriginMutationServerProxy(req);

          if (scenario.expectAllowed) {
            expect(middlewareVerdict).toBeNull();
            expect(serverProxyVerdict).toBeNull();
          } else {
            expect(middlewareVerdict?.status).toBe(403);
            expect(serverProxyVerdict?.status).toBe(403);
          }
          // The two independently-implemented layers must never disagree.
          expect(middlewareVerdict === null).toBe(serverProxyVerdict === null);
        });
      }
    });
  }

  // --- HEAD/OPTIONS: not mutating methods, both layers must be a pure no-op,
  // regardless of how hostile the Origin/Sec-Fetch-Site headers look. -------
  for (const method of NON_MUTATING_METHODS) {
    it(`${method} is untouched even with a cross-site Sec-Fetch-Site and no Origin`, () => {
      const req = request(method, { "Sec-Fetch-Site": "cross-site" });

      expect(requireSameOriginMutationMiddleware(req)).toBeNull();
      expect(requireSameOriginMutationServerProxy(req)).toBeNull();
    });
  }

  // --- env rotation at request time: the allowlist must be read live per
  // request, never cached at module load, in BOTH layers. -------------------
  describe("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS is read fresh on every request", () => {
    beforeEach(() => {
      vi.unstubAllEnvs();
    });

    it("an origin becomes allowed the instant it's configured, and stops being allowed the instant it's removed", () => {
      const req = request("POST", { Origin: REMOTE_ORIGIN });

      // Before configuration: denied.
      expect(requireSameOriginMutationMiddleware(req)?.status).toBe(403);
      expect(requireSameOriginMutationServerProxy(req)?.status).toBe(403);

      // Configured mid-test: the very next call allows it, no reload needed.
      vi.stubEnv("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS", REMOTE_ORIGIN);
      expect(requireSameOriginMutationMiddleware(req)).toBeNull();
      expect(requireSameOriginMutationServerProxy(req)).toBeNull();

      // Rotated to a different origin: the old one is denied again.
      vi.stubEnv("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS", "https://another.example.ts.net");
      expect(requireSameOriginMutationMiddleware(req)?.status).toBe(403);
      expect(requireSameOriginMutationServerProxy(req)?.status).toBe(403);
    });
  });
});
