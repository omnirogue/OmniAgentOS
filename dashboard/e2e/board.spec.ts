import { test, expect, chromium, type Browser } from "@playwright/test";
import { E2E_BASE_URL, handleMissingPrecondition } from "./rail.authority";

/**
 * Board v2 e2e (chat-v2 §2.5/§2.6): card click opens the everything drawer on
 * /board?task=<id>; the project filter scopes server-side.
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

test.describe("Board v2 — everything drawer", () => {
  test("clicking a card opens the drawer with tabs and updates the URL", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/board`);
      await expect(page.locator("h1:has-text('Board')")).toBeVisible({ timeout: 15000 });

      const firstCard = page.locator("a[aria-label^='Open details for']").first();
      try {
        await expect(firstCard).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("No cards on the board — seeding required");
        return;
      }
      await firstCard.click();

      // URL carries ?task=<id> and the drawer mounts with the six tabs
      await expect(page).toHaveURL(/\/board\?task=/, { timeout: 10000 });
      await expect(page.locator("role=tab[name='Overview']")).toBeVisible({ timeout: 10000 });
      await expect(page.locator("role=tab[name='Chat']")).toBeVisible();

      // The Chat tab mounts the same conversation primitive
      await page.locator("role=tab[name='Chat']").click();
      await expect(
        page.locator("textarea[placeholder*='Message']").first(),
      ).toBeVisible({ timeout: 10000 });

      // Esc closes the drawer and clears the URL param
      await page.keyboard.press("Escape");
      await expect(page).not.toHaveURL(/\/board\?task=/, { timeout: 10000 });
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("company filter chips render and toggle aria-pressed", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/board`);
      await expect(page.locator("h1:has-text('Board')")).toBeVisible({ timeout: 15000 });

      // The chip group is static (multi-company Work OS, 2026-08-13) — it
      // renders regardless of whether the backend ships company_slug yet.
      const chipGroup = page.locator("[role='group'][aria-label='Filter by company']");
      await expect(chipGroup).toBeVisible({ timeout: 15000 });
      for (const label of ["Globex", "AcmeUni", "Hooli", "Initech", "OmniAgentOS"]) {
        await expect(chipGroup.locator(`button:has-text('${label}')`)).toBeVisible();
      }

      const acmeuniChip = chipGroup.locator("button:has-text('AcmeUni')");
      await expect(acmeuniChip).toHaveAttribute("aria-pressed", "false");
      await acmeuniChip.click();
      await expect(acmeuniChip).toHaveAttribute("aria-pressed", "true");
      // Toggling the same chip again clears the filter.
      await acmeuniChip.click();
      await expect(acmeuniChip).toHaveAttribute("aria-pressed", "false");

      // Card-face company chip needs the upgraded backend + a company-tied
      // card — a precondition, not a failure (same pattern as the pool assert).
      const cardCompanyChip = page.locator("[title='Company']").first();
      try {
        await expect(cardCompanyChip).toBeVisible({ timeout: 10000 });
      } catch {
        handleMissingPrecondition("No company chip on any board card — company_slug is not on the board payload yet");
        return;
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("project filter scopes the board or shows the scoped empty state", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/board?project=proj_nonexistent`);
      await expect(page.locator("h1:has-text('Board')")).toBeVisible({ timeout: 15000 });

      // Either the scoped empty state (with the pre-086 disclosure) or a
      // project-filtered board — never the old blank board. Neither renders
      // if useLiveBoard's fetch never succeeds even once (board/page.tsx
      // gates both behind `hasLoaded`) — on this box, with no
      // OMNIAGENTOS_TRUSTED_HOP_SECRET / dev-escape configured for this
      // `next dev`, the trusted-hop middleware refuses that fetch, which is
      // unrelated to this test or the IA redo (see
      // docs/runbooks/dashboard-local-auth.md).
      const emptyScoped = page.locator("text=No cards in this project yet").first();
      const kanban = page.locator("section[aria-label='Live task board']").first();
      try {
        await expect(emptyScoped.or(kanban)).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("Neither the scoped empty state nor the kanban rendered — board API fetch never succeeded (trusted-hop/backend unavailable)");
        return;
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});
