import { test, expect, chromium, type Browser, type Page } from "@playwright/test";
import { E2E_BASE_URL } from "./rail.authority";

/**
 * Plan P4 ship gate (Dashboard-Rationalization 2026-08-04 §4): the board and
 * the phone home must be workable at 390×844 — the Sol re-run catch was that a
 * multi-column kanban is unusable unadapted. These assertions are layout
 * truths, not data truths: they hold whether or not the API has cards.
 */

const PHONE = { width: 390, height: 844 } as const;

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

async function bodyHorizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const root = document.scrollingElement ?? document.documentElement;
    return root.scrollWidth - root.clientWidth;
  });
}

test.describe("Phone 390×844 — P4 mobile validation", () => {
  test("/: bottom nav present, page body never scrolls horizontally", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage({ viewport: { ...PHONE } });
      await page.goto(`${E2E_BASE_URL}/`);

      const bottomNav = page.locator("nav[aria-label='Primary (compact)']");
      await expect(bottomNav).toBeVisible({ timeout: 15000 });

      // The nav carries the four fixed surfaces (Status/Board/Inbox/Sessions,
      // new 10-page IA, P0 shell rebuild 2026-08-14) plus the More reveal.
      await expect(bottomNav.locator("text=Status")).toBeVisible();
      await expect(bottomNav.locator("text=Board")).toBeVisible();
      await expect(
        bottomNav.locator("[aria-label='More — open navigation']"),
      ).toBeVisible();

      expect(await bodyHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/board: renders at phone width with columns scrolling inside their own scroller", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage({ viewport: { ...PHONE } });
      await page.goto(`${E2E_BASE_URL}/board`);

      await expect(page.locator("h1:has-text('Board')")).toBeVisible({
        timeout: 15000,
      });
      await expect(
        page.locator("nav[aria-label='Primary (compact)']"),
      ).toBeVisible({ timeout: 15000 });

      // Wide content (the column rail) must scroll inside its own container —
      // the page body itself must not grow a horizontal scrollbar.
      expect(await bodyHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});
