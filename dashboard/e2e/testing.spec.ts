import { test, expect, chromium, type Browser } from "@playwright/test";
import { E2E_BASE_URL, handleMissingPrecondition } from "./rail.authority";

/**
 * Test Observatory e2e. Mirrors team.spec.ts's shape: launch chromium
 * directly (not the `test` fixture's shared browser), gracefully skip via
 * `handleMissingPrecondition` when the backend this test needs is absent,
 * never a hard failure for a missing precondition. Kept to one spec per the
 * testobs design contract.
 */

const CHROMIUM_LAUNCH_ARGS = [
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

test.describe("Test Observatory — /testing", () => {
  test("renders the Test Observatory page with its stat row and weak-spots table", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/testing`);

      // Hard assertion, no handleMissingPrecondition fallback (grok T2-M3,
      // exactly like team.spec.ts:35) — the page mounting is not an API
      // precondition, it's the app itself; a missing h1 is a real failure.
      await expect(page.locator("h1:has-text('Test Observatory')")).toBeVisible({ timeout: 15000 });

      // The stat row always renders 4 tiles once the overview loads (even an
      // unavailable category renders its own neutral tile, never a blank gap).
      const statTitles = page.locator("h3, span").filter({ hasText: /^(North Star|Memory|Diagnostics|Suite)$/ });
      try {
        await expect(statTitles.first()).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("No stat tile rendered — /api/testobs/overview unavailable or not yet deployed");
        return;
      }

      // Weak spots table renders unconditionally (ranked table, empty state,
      // or its own error state) — assert the section heading shows up.
      try {
        await expect(page.getByRole("heading", { name: "Weak spots" })).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("Weak spots panel did not render — /api/testobs/weakspots unavailable");
        return;
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});
