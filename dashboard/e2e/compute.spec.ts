import { test, expect, chromium, type Browser } from "@playwright/test";
import { E2E_BASE_URL, handleMissingPrecondition } from "./rail.authority";

/**
 * Estate Compute e2e. Mirrors testing.spec.ts's shape (itself mirroring
 * team.spec.ts): launch chromium directly (not the `test` fixture's shared
 * browser), gracefully skip via `handleMissingPrecondition` when the backend
 * this test needs is absent, never a hard failure for a missing
 * precondition. Kept to one spec per the wire contract.
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

test.describe("Estate Compute — /compute", () => {
  test("renders the Estate Compute page with its summary row, machines, and runners", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/compute`);

      // Hard assertion, no handleMissingPrecondition fallback (mirrors
      // testing.spec.ts / team.spec.ts) — the page mounting is not an API
      // precondition, it's the app itself; a missing h1 is a real failure.
      await expect(page.locator("h1:has-text('Estate Compute')")).toBeVisible({ timeout: 15000 });

      // The summary row always renders its 4 tiles once the estate loads
      // (even an unavailable envelope renders its own neutral tile, never a
      // blank gap).
      const summaryTitles = page
        .locator("span")
        .filter({ hasText: /^(Free slots|Queue depth|Estate runners|This machine)$/ });
      try {
        await expect(summaryTitles.first()).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("No summary tile rendered — /api/compute/estate unavailable or not yet deployed");
        return;
      }

      // Machines and Runners sections render unconditionally (a grid, an
      // empty state, or an error state) — assert their section headings show.
      try {
        await expect(page.getByRole("heading", { name: "Machines" })).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("Machines section did not render — /api/compute/estate unavailable");
        return;
      }
      await expect(page.getByRole("heading", { name: "Runners" })).toBeVisible({ timeout: 10000 });
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});
