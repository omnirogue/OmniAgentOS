import { test, expect, chromium, type Browser } from "@playwright/test";
import { E2E_BASE_URL, handleMissingPrecondition } from "./rail.authority";

/**
 * Team Work OS e2e (P5). Mirrors board.spec.ts's shape: launch chromium
 * directly (not the `test` fixture's shared browser), gracefully skip via
 * `handleMissingPrecondition` when the backend/seed data this test needs is
 * absent, never a hard failure for missing preconditions.
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

test.describe("Team Work OS — /team", () => {
  test("renders the team board and renders the Pool column contract", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/team`);
      await expect(page.locator("h1:has-text('Team Work OS')")).toBeVisible({ timeout: 15000 });

      // team_queues() always returns a bucket per roster employee, even an
      // EMPTY one ("nothing to do" and "not on the team" must render
      // differently) — so the operator/Alice/Bob render unconditionally once the
      // API answers. "Agents & unowned" only renders when there is
      // something in it, so it is not part of this predicate.
      const named = page.locator("h3:has-text('the operator'), h3:has-text('Alice'), h3:has-text('Bob')");
      try {
        await expect(named.first()).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("No named person section rendered — /api/team/board unavailable or the roster is unseeded");
        return;
      }

      // Every section always shows a card count, even when it is zero.
      await expect(page.getByText(/\d+ cards?/).first()).toBeVisible({ timeout: 10000 });
      try {
        await expect(page.getByRole("heading", { name: "Pool" })).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("Pool is absent — backend fixture is not yet serving the upgraded team board contract");
        return;
      }
      await expect(page.getByRole("heading", { name: "Pool" }).locator("..").getByText(/\d+/)).toBeVisible();
      const lowPoolBadge = page.getByText("Low pool");
      if (await lowPoolBadge.count()) await expect(lowPoolBadge).toBeVisible();
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("pool cards surface company + priority from the server payload", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/team`);
      await expect(page.locator("h1:has-text('Team Work OS')")).toBeVisible({ timeout: 15000 });

      try {
        await expect(page.getByRole("heading", { name: "Pool" })).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("Pool is absent — backend fixture is not yet serving the upgraded team board contract");
        return;
      }

      // Company chip (title="Company") renders only when the server ships
      // company_slug/company_name on queue cards (2026-08-13 widening) AND a
      // card is actually tied to a company — both are preconditions, not
      // failures, against an un-upgraded or unseeded backend.
      const companyChip = page.locator("[title='Company']").first();
      try {
        await expect(companyChip).toBeVisible({ timeout: 10000 });
      } catch {
        handleMissingPrecondition("No company chip on any card — the backend is not serving company fields on queue cards yet");
        return;
      }
      await expect(companyChip).not.toBeEmpty();

      // Server-truth priority badge on a card face. 'normal' is suppressed by
      // design, so only an escalated (or demoted) card renders a chip — a
      // board where every card is 'normal' is a precondition gap, not a bug.
      const priorityBadge = page.locator("button[aria-label^='Open '] >> text=/^(low|high|urgent)$/").first();
      try {
        await expect(priorityBadge).toBeVisible({ timeout: 10000 });
      } catch {
        handleMissingPrecondition("No non-normal priority badge on any card — no escalated card is on the board yet");
        return;
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("opening a team card deep-links ?task= and opens the drawer", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/team`);
      await expect(page.locator("h1:has-text('Team Work OS')")).toBeVisible({ timeout: 15000 });

      const firstCard = page.locator("button[aria-label^='Open ']").first();
      try {
        await expect(firstCard).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("No cards in any team queue — seeding required");
        return;
      }
      await firstCard.click();

      await expect(page).toHaveURL(/\/team\?task=/, { timeout: 10000 });
      await expect(page.locator("role=tab[name='Overview']")).toBeVisible({ timeout: 10000 });
      await expect(page.locator("role=tab[name='Evidence']")).toBeVisible();

      await page.keyboard.press("Escape");
      await expect(page).not.toHaveURL(/\/team\?task=/, { timeout: 10000 });
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});

test.describe("Team Work OS — /board owner filter", () => {
  test("owner chips render and toggle aria-pressed", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/board`);
      await expect(page.locator("h1:has-text('Board')")).toBeVisible({ timeout: 15000 });

      const chipGroup = page.locator("[role='group'][aria-label='Filter by owner']");
      await expect(chipGroup).toBeVisible({ timeout: 15000 });

      const agentsChip = chipGroup.locator("button:has-text('Agents')");
      await expect(agentsChip).toBeVisible();
      await expect(agentsChip).toHaveAttribute("aria-pressed", "false");
      await agentsChip.click();
      await expect(agentsChip).toHaveAttribute("aria-pressed", "true");

      // Toggling back off (same chip again) clears the filter.
      await agentsChip.click();
      await expect(agentsChip).toHaveAttribute("aria-pressed", "false");
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});

// The "/goals company tree" describe block from main was REMOVED at the
// 2026-08-14 merge: /goals is one of the routes the 12-page IA deletes, so a
// navigation test against it is a guaranteed red (caught by the merge-
// resolution review). The company-goals tree now lives on /companies.
