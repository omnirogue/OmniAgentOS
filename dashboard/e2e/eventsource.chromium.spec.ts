/**
 * Packet-required real Chromium EventSource tests (B06).
 *
 * Uses Playwright Chromium with native in-page `EventSource` against isolated
 * ephemeral loopback ports. Never touches live dashboard :3000 / product :8485.
 *
 * Chromium launches with --single-process/--no-zygote so multi-process Mach
 * rendezvous is avoided in constrained agent environments. Headless Chromium
 * reports a HeadlessChrome user agent string — tests assert that real browser
 * UA (not Node), without claiming a non-headless browser.
 *
 * Cases:
 * 1) Same-origin supplemental proof (HTML + SSE one origin; no CORS headers).
 * 2) True product-origin cross-origin EventSource with EXPLICIT allowed Origin
 *    (never Access-Control-Allow-Origin: *).
 * 3) Mismatched browser origin is rejected with HTTP 403 and no ACAO.
 *
 * Output is isolated and portable (see runtimePaths.ts).
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { test, expect, chromium, type Browser } from "@playwright/test";
import { B06_E2E_OUT } from "./runtimePaths";
import {
  probeNativeEventSource,
  startProductOriginCrossOriginFixture,
  startSseFixture,
  stopDualOriginFixture,
  stopSseFixture,
} from "./sseHarness";

/** Launch args required for real Chromium under agent seatbelt constraints. */
export const CHROMIUM_LAUNCH_ARGS = [
  "--no-sandbox",
  "--disable-setuid-sandbox",
  "--single-process",
  "--no-zygote",
  "--disable-gpu",
  "--disable-dev-shm-usage",
] as const;

async function launchChromium(): Promise<Browser> {
  return chromium.launch({
    headless: true,
    chromiumSandbox: false,
    args: [...CHROMIUM_LAUNCH_ARGS],
  });
}

test.describe("Playwright Chromium native EventSource (B06)", () => {
  test("same-origin: connects, receives run.updated + eventbus.status, surfaces stream failure", async () => {
    mkdirSync(B06_E2E_OUT, { recursive: true });
    const fixture = await startSseFixture();
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      // Same-origin page so native EventSource needs no CORS headers.
      await page.goto(fixture.baseUrl + "/");
      const result = await probeNativeEventSource(
        page,
        "/api/events?after_id=0&types=run.updated,eventbus.status",
        { corsMode: "same-origin" },
      );

      writeFileSync(
        path.join(B06_E2E_OUT, "eventsource-chromium-result.json"),
        JSON.stringify(
          {
            harness: "playwright-chromium",
            mode: "same-origin",
            note: "Supplemental same-origin proof; product-origin cross-origin is separate.",
            baseUrl: fixture.baseUrl,
            port: fixture.port,
            allowedOrigin: fixture.allowedOrigin,
            corsWildcard: false,
            result,
            at: new Date().toISOString(),
          },
          null,
          2,
        ),
      );

      expect(result.opened, "native EventSource should open").toBe(true);
      expect(result.receivedType).toBe("run.updated");
      expect(result.eventBusState).toBe("degraded");
      expect(result.errorSeen, "missing stream should surface onerror").toBe(true);
      // Truthful UA claim: headless Chromium reports HeadlessChrome (or Chrome/).
      expect(result.userAgent ?? "").toMatch(/HeadlessChrome|Chrome\//);
      expect(result.userAgent ?? "", "must not be Node.js").not.toMatch(/^Node\.js/);
      expect(result.corsMode).toBe("same-origin");
    } finally {
      await browser?.close().catch(() => undefined);
      await stopSseFixture(fixture);
    }
  });

  test("product-origin cross-origin: explicit allowed Origin, never wildcard CORS", async () => {
    mkdirSync(B06_E2E_OUT, { recursive: true });
    const dual = await startProductOriginCrossOriginFixture();
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();

      // Capture response CORS headers from the API origin.
      let seenAcao: string | null = null;
      page.on("response", (response) => {
        if (response.url().includes("/api/events")) {
          seenAcao = response.headers()["access-control-allow-origin"] ?? null;
        }
      });

      // Navigate to the product origin page (distinct from the SSE API origin).
      await page.goto(dual.productOrigin + "/");
      expect(new URL(page.url()).origin).toBe(dual.productOrigin);
      expect(dual.productOrigin).not.toBe(dual.apiOrigin);

      const eventsUrl = `${dual.apiOrigin}/api/events?after_id=0&types=run.updated,eventbus.status`;
      const result = await probeNativeEventSource(page, eventsUrl, {
        corsMode: "cross-origin",
      });

      writeFileSync(
        path.join(B06_E2E_OUT, "eventsource-chromium-cross-origin-result.json"),
        JSON.stringify(
          {
            harness: "playwright-chromium",
            mode: "product-origin-cross-origin",
            productOrigin: dual.productOrigin,
            apiOrigin: dual.apiOrigin,
            allowedOrigin: dual.api.allowedOrigin,
            accessControlAllowOrigin: seenAcao,
            corsWildcard: seenAcao === "*",
            result,
            at: new Date().toISOString(),
          },
          null,
          2,
        ),
      );

      expect(result.opened, "cross-origin EventSource should open with explicit origin").toBe(
        true,
      );
      expect(result.receivedType).toBe("run.updated");
      expect(result.eventBusState).toBe("degraded");
      expect(result.errorSeen).toBe(true);
      expect(result.documentOrigin).toBe(dual.productOrigin);
      expect(result.eventSourceUrl).toContain(dual.apiOrigin);
      expect(result.userAgent ?? "").toMatch(/HeadlessChrome|Chrome\//);
      // Explicit product origin only — never wildcard.
      expect(seenAcao, "ACAO must be the explicit product origin").toBe(dual.productOrigin);
      expect(seenAcao).not.toBe("*");
      expect(dual.api.allowedOrigin).toBe(dual.productOrigin);
      expect(dual.api.allowedOrigin).not.toBe("*");
      expect(dual.api.observations).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            origin: dual.productOrigin,
            status: 200,
            accessControlAllowOrigin: dual.productOrigin,
          }),
        ]),
      );
    } finally {
      await browser?.close().catch(() => undefined);
      await stopDualOriginFixture(dual);
    }
  });

  test("mismatched browser origin: receives 403, no ACAO, and EventSource never opens", async () => {
    mkdirSync(B06_E2E_OUT, { recursive: true });
    const dual = await startProductOriginCrossOriginFixture();
    const mismatchedPage = await startSseFixture();
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();

      await page.goto(mismatchedPage.baseUrl + "/");
      expect(new URL(page.url()).origin).toBe(mismatchedPage.baseUrl);
      expect(mismatchedPage.baseUrl).not.toBe(dual.productOrigin);

      const eventsUrl = `${dual.apiOrigin}/api/events?after_id=0&types=run.updated,eventbus.status`;
      const result = await probeNativeEventSource(page, eventsUrl, {
        corsMode: "cross-origin",
      });
      const denied = dual.api.observations.find(
        (observation) =>
          observation.url.startsWith("/api/events") &&
          observation.origin === mismatchedPage.baseUrl,
      );

      writeFileSync(
        path.join(B06_E2E_OUT, "eventsource-chromium-denied-origin-result.json"),
        JSON.stringify(
          {
            harness: "playwright-chromium",
            mode: "mismatched-origin-negative-control",
            requestOrigin: mismatchedPage.baseUrl,
            allowedOrigin: dual.productOrigin,
            apiOrigin: dual.apiOrigin,
            status: denied?.status ?? null,
            accessControlAllowOrigin:
              denied?.accessControlAllowOrigin ?? null,
            result,
            at: new Date().toISOString(),
          },
          null,
          2,
        ),
      );

      expect(
        denied,
        "server must observe the native browser's mismatched Origin",
      ).toBeDefined();
      expect(denied?.status, "API must reject the mismatched Origin").toBe(403);
      expect(
        denied?.accessControlAllowOrigin,
        "denied response must not grant CORS",
      ).toBeNull();
      expect(result.opened).toBe(false);
      expect(result.receivedType).toBeNull();
      expect(result.eventBusState).toBeNull();
      expect(result.errorSeen).toBe(true);
    } finally {
      await browser?.close().catch(() => undefined);
      await Promise.all([
        stopDualOriginFixture(dual),
        stopSseFixture(mismatchedPage),
      ]);
    }
  });
});
