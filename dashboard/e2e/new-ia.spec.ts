import { test, expect, chromium, type Browser } from "@playwright/test";
import { E2E_BASE_URL } from "./rail.authority";

/**
 * New-IA smoke suite (P7, 10-page-nav sweep, 2026-08-14). Covers the shell
 * the P0-P6 redo landed: the flat 12-page nav (AppShell's NAV_LINKS), the
 * /approvals -> /inbox redirect, and one structural check per page that owns
 * a new `/api/local/*` route (Status, Skills, Tests, Repos) plus Companies.
 *
 * Every data-backed assertion below is written to accept either the real
 * rendered structure OR the page's own explicit named ErrorState as a pass
 * (never a specific live number) — the contract these routes make is
 * honest-render, not network success. In particular, this box's own
 * trusted-hop middleware (`src/middleware.ts`) refuses every `/api/*` call
 * unless the `next dev` process was started with
 * `OMNIAGENTOS_TRUSTED_HOP_SECRET` + the three dev-escape variables (see
 * docs/runbooks/dashboard-local-auth.md) — nothing in this harness's
 * `webServer.command` sets those, so a plain `npx playwright test` renders
 * every data section as its ErrorState. That is a legitimate observed state
 * for these assertions, not a failure.
 *
 * F06 (2026-08-14): these assertions used to fall back to
 * `handleMissingPrecondition` (skip) the moment the real-content locator
 * timed out, WITHOUT checking that anything actually rendered — a genuinely
 * blank page (a real regression) and an honest ErrorState both produced the
 * exact same silent skip. Every site below instead asserts
 * `realContent.or(namedErrorState)` directly: real content is a pass, the
 * page's own explicit ErrorState is ALSO an asserted pass (both are honest
 * renders), and if NEITHER shows up the assertion itself times out and the
 * test fails outright -- a blank page can no longer hide behind a skip.
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

/** The flat 12-page IA, in AppShell's NAV_LINKS order. Team and Testing
 * joined at the 2026-08-14 merge with main's parallel work (dev-accountability
 * team page fb8f78a3f; API-backed testing observatory f542cc516). */
const NAV_ROUTES = [
  "/",
  "/companies",
  "/board",
  "/team",
  "/inbox",
  "/sessions",
  "/cash",
  "/skills",
  "/repos",
  "/tests",
  "/testing",
  "/files",
] as const;

test.describe("New IA — nav shell (P7)", () => {
  test("every one of the 12 nav routes loads: HTTP 200, an h1, main content, no error boundary", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      for (const route of NAV_ROUTES) {
        await test.step(route, async () => {
          const response = await page.goto(`${E2E_BASE_URL}${route}`);
          expect(response?.ok(), `${route} responded ${response?.status()}`).toBe(true);

          // The global error boundary (src/app/global-error.tsx) replaces the
          // ENTIRE document with its own <html><body> when a render throws —
          // AppShell (and the h1/main below) never mount alongside it. So an
          // h1 inside a visible <main> already proves the boundary did not
          // fire; a separate text check for its "Something went wrong" copy
          // would false-positive on an ordinary per-section ErrorState,
          // which defaults to that exact same title (design/ErrorState.tsx).
          await expect(page.locator("h1").first()).toBeVisible({ timeout: 15000 });
          await expect(page.locator("main").first()).toBeVisible();
        });
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/approvals?tab=decisions redirects to /inbox, preserving the query string", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/approvals?tab=decisions`);
      await expect(page).toHaveURL(/\/inbox\?tab=decisions$/, { timeout: 15000 });
      await expect(page.locator("h1").first()).toBeVisible({ timeout: 15000 });
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("Status (/) renders the six section headings", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/`);
      await expect(page.locator("h1:has-text('Status')")).toBeVisible({ timeout: 15000 });

      const sectionTitles = ["Loops", "Gate", "Queue", "Landings", "Recycler", "Alerts"];
      const firstSection = page.locator(`h2:has-text("${sectionTitles[0]}")`);
      const errorState = page.locator("text=Could not load status");
      // Real content OR the page's own explicit ErrorState is the asserted
      // pass; neither rendering fails the test outright (see file doc comment).
      await expect(firstSection.or(errorState)).toBeVisible({ timeout: 15000 });
      if (await errorState.count()) return;
      for (const title of sectionTitles.slice(1)) {
        await expect(page.locator(`h2:has-text("${title}")`)).toBeVisible();
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/companies shows the operating-companies grid and the platform row", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/companies`);
      await expect(page.locator("h1:has-text('Companies')")).toBeVisible({ timeout: 15000 });

      const operatingSection = page.locator("h2:has-text('Operating companies')");
      // design/ErrorState.tsx's SPECIFIC signature (.ds-state + role=alert,
      // scoped inside main) -- an unrelated alert banner elsewhere on the page
      // (e.g. a status queue banner) must NOT satisfy this assertion. The
      // page's own honest render when
      // the same-origin proxy to the Companies API is refused or errors.
      const errorState = page.locator('main .ds-state[role="alert"]');
      await expect(operatingSection.or(errorState)).toBeVisible({ timeout: 15000 });
      if (await errorState.count()) return;

      // Structure only, never a specific count (companies API is live data):
      // either the four branded companies rendered as cards, or the
      // catalog's own explicit "no operating companies" empty state — both
      // honest renders.
      const brandCards = page.locator("h3").filter({ hasText: /.+/ });
      const emptyCatalog = page.locator("text=No operating companies found");
      await expect(brandCards.first().or(emptyCatalog)).toBeVisible({ timeout: 10000 });

      // The platform row (OmniAgentOS) is conditional on the API returning a
      // platform view at all — present or absent are both legitimate; only
      // assert on it when the section itself exists.
      const platformSection = page.locator("h2:has-text('OmniAgentOS')");
      if (await platformSection.count()) {
        await expect(platformSection.first()).toBeVisible();
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/skills shows a library skill count greater than zero", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/skills`);
      await expect(page.locator("h1:has-text('Skills')")).toBeVisible({ timeout: 15000 });

      const libraryHeading = page.locator("h2", { hasText: /^Skill library \(\d+\)$/ });
      // design/ErrorState.tsx's SPECIFIC signature (.ds-state + role=alert,
      // scoped inside main) -- an unrelated alert banner elsewhere on the page
      // (e.g. a status queue banner) must NOT satisfy this assertion. The
      // page's own honest render when
      // GET /api/local/skills-extra is refused or errors.
      const errorState = page.locator('main .ds-state[role="alert"]');
      await expect(libraryHeading.or(errorState)).toBeVisible({ timeout: 15000 });
      if (await errorState.count()) return;

      const text = await libraryHeading.textContent();
      const match = text?.match(/\((\d+)\)/);
      const count = match ? Number(match[1]) : 0;
      expect(count, `skill library count parsed from "${text}"`).toBeGreaterThan(0);

      // OC7 (cross-lineage review round 2, 2026-08-14): count > 0 alone
      // would still pass with every entry degraded (e.g. everything
      // "unreadable (error)" from a broad TCC/exec regression) — a category
      // heading is the ONLY <h3> the skills page renders (skills/page.tsx),
      // so this stays a precise, non-brittle check. On a healthy box: at
      // least one entry is non-degraded, and zero categories carry an
      // "unreadable"/"refused" marker.
      const categoryHeadings = await page.getByRole("heading", { level: 3 }).allTextContents();
      expect(categoryHeadings.length, "at least one category heading rendered").toBeGreaterThan(0);
      const degraded = categoryHeadings.filter((c) => c.startsWith("unreadable (") || c === "out-of-root (refused)");
      expect(degraded, `degraded categories found: ${JSON.stringify(degraded)}`).toHaveLength(0);
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/tests renders the train board section", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/tests`);
      await expect(page.locator("h1:has-text('Tests')")).toBeVisible({ timeout: 15000 });

      // Train board is a local-only read (merge-gate receipts on this box's
      // own disk, no gh call) gated behind the SAME payload as the
      // gh-backed CI/Landings sections below it — GET /api/local/tests
      // isolates gh failures into their own `{ error }` sub-sections, so the
      // train board renders whenever the request reaches the route handler
      // at all.
      const trainBoard = page.locator("h2:has-text('Train board')");
      // design/ErrorState.tsx's SPECIFIC signature (.ds-state + role=alert,
      // scoped inside main) -- an unrelated alert banner elsewhere on the page
      // (e.g. a status queue banner) must NOT satisfy this assertion. The
      // page's own honest render when
      // GET /api/local/tests is refused (trusted-hop).
      const errorState = page.locator('main .ds-state[role="alert"]');
      await expect(trainBoard.or(errorState)).toBeVisible({ timeout: 15000 });
      if (await errorState.count()) return;

      // Either real gate-run rows or the route's own explicit empty state —
      // both honest renders, never a specific run count.
      const runsRow = page.locator("table.ds-table tbody tr").first();
      const emptyRuns = page.locator("text=No gate runs");
      await expect(runsRow.or(emptyRuns)).toBeVisible({ timeout: 10000 });
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/repos renders the three GitHub owner sections (each live or its own explicit unavailable state)", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/repos`);
      await expect(page.locator("h1:has-text('Repositories')")).toBeVisible({ timeout: 15000 });

      // OwnerSection (features/repos/ReposDashboard.tsx) always renders its
      // <h2>{owner}</h2> unconditionally, whether that owner's `gh repo list`
      // succeeded or errored — the per-owner `unavailable: ...` text sits
      // alongside it, never in place of it. But the whole three-owner grid
      // is itself gated behind the page-level fetch to GET /api/local/repos
      // succeeding at all, which this box's trusted-hop middleware can
      // refuse outright (see file doc comment) — so tolerate the page-level
      // ErrorState as the third valid outcome.
      const owners = ["example-org", "Globex", "initech"] as const;
      const firstOwnerHeading = page.locator(`h2:has-text("${owners[0]}")`);
      const pageError = page.locator("text=Could not load repository inventory");
      // Real content OR the page-level ErrorState is the asserted pass; if
      // NEITHER shows up (GET /api/local/repos never resolved to either
      // outcome) the assertion itself times out and the test fails outright.
      await expect(firstOwnerHeading.or(pageError)).toBeVisible({ timeout: 15000 });
      if (await pageError.count()) return;
      for (const owner of owners) {
        await expect(page.locator(`h2:has-text("${owner}")`)).toBeVisible();
      }
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });
});
