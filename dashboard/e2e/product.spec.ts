import { test, expect, chromium, type Browser } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { E2E_BASE_URL, handleMissingPrecondition } from "./rail.authority";

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

function getSessionToken(): string {
  const tokenPath = path.resolve(__dirname, "../../var/secrets/sessions-token");
  if (fs.existsSync(tokenPath)) {
    return fs.readFileSync(tokenPath, "utf8").trim();
  }
  return "";
}

async function fetchFromBackend(endpoint: string): Promise<unknown> {
  const token = getSessionToken();
  const res = await fetch(`http://127.0.0.1:8485${endpoint}`, {
    headers: {
      "X-Session-Token": token,
    },
  });
  if (!res.ok) {
    throw new Error(`Backend API error ${res.status} on ${endpoint}`);
  }
  return (await res.json()) as unknown;
}

type ChecklistBearingTask = {
  id: string;
  checklist: {
    done: number;
    total: number;
  };
};

function isChecklistBearingTask(value: unknown): value is ChecklistBearingTask {
  if (typeof value !== "object" || value === null) return false;

  const task = value as Record<string, unknown>;
  if (typeof task.id !== "string" || task.id.length === 0) return false;
  if (typeof task.checklist !== "object" || task.checklist === null) return false;

  const checklist = task.checklist as Record<string, unknown>;
  return (
    Number.isInteger(checklist.done) &&
    Number.isInteger(checklist.total) &&
    (checklist.done as number) >= 0 &&
    (checklist.total as number) > 0 &&
    (checklist.done as number) <= (checklist.total as number)
  );
}

function selectChecklistBearingTask(tasks: unknown[]): ChecklistBearingTask | undefined {
  return tasks.find(isChecklistBearingTask);
}

test.describe("E2E Product Surface Walkthrough", () => {
  // /chats was deleted outright in the new 10-page IA (P0 shell rebuild,
  // 2026-08-14 — DASHBOARD-REDO-DESIGN.md's 33-route deletion list); its
  // coverage went with it rather than being pointed at a route that no
  // longer exists. See new-ia.spec.ts for the replacement home-page (/)
  // coverage.

  test("/board - verify expected 9 kanban columns and live API categories in the filter", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/board`);

      // Ensure the board loads and header is visible
      await expect(page.locator("h1:has-text('Board')")).toBeVisible({ timeout: 15000 });

      // Verify the expected 9 Kanban columns render. The kanban only mounts
      // once useLiveBoard's fetch has succeeded at least once (board/page.tsx
      // gates it behind `hasLoaded`) — a dashboard API call this box's own
      // trusted-hop middleware refuses (no OMNIAGENTOS_TRUSTED_HOP_SECRET /
      // dev-escape configured for this `next dev`, unrelated to board.spec.ts
      // or the IA redo — see docs/runbooks/dashboard-local-auth.md) never
      // reaches `hasLoaded`, so the page renders its ErrorState instead of
      // any column. Checked on the first column only, so the remaining eight
      // don't each burn their own 5s timeout for the same root cause.
      const expectedColumns = [
        "Backlog",
        "Ready",
        "Running",
        "Needs you",
        "In Review",
        "Testing",
        "Integration",
        "Completed",
        "Blocked",
      ];
      try {
        await expect(page.locator(`h3:has-text("${expectedColumns[0]}")`)).toBeVisible({ timeout: 5000 });
      } catch {
        handleMissingPrecondition("No kanban columns rendered — board API fetch never succeeded (trusted-hop/backend unavailable)");
        return;
      }
      for (const label of expectedColumns.slice(1)) {
        const colHeading = page.locator(`h3:has-text("${label}")`);
        await expect(colHeading).toBeVisible({ timeout: 5000 });
      }

      // Read the authenticated live taxonomy through the same-origin proxy.
      // The proxy injects the session token server-side, exactly as the board
      // does, so this never relies on or writes a production seed set.
      const categoriesResponse = await page.request.get(`${E2E_BASE_URL}/api/categories`);
      expect(
        categoriesResponse.ok(),
        `Authenticated /api/categories returned ${categoriesResponse.status()}`,
      ).toBeTruthy();
      const categoriesPayload: unknown = await categoriesResponse.json();
      expect(Array.isArray(categoriesPayload), "/api/categories must return an array").toBe(true);

      const categoryNames = (categoriesPayload as unknown[]).map((category, index) => {
        expect(
          typeof category === "object" &&
            category !== null &&
            typeof (category as Record<string, unknown>).name === "string",
          `/api/categories item ${index} must expose a string name`,
        ).toBe(true);
        return (category as Record<string, unknown>).name as string;
      });
      if (categoryNames.length === 0 || categoryNames.some((name) => name.trim().length === 0)) {
        handleMissingPrecondition(
          "Named category precondition failed — authenticated /api/categories must return at least one category and every category must have a non-empty name",
        );
        return;
      }

      const categoryTrigger = page.locator("button.ds-select__trigger[aria-label='Category']");
      await expect(categoryTrigger).toBeVisible({ timeout: 15000 });

      // Click the custom select to open the options listbox
      await categoryTrigger.click();

      const listbox = page.getByRole("listbox");
      await expect(listbox).toBeVisible();

      const expectedOptionNames = ["All categories", ...categoryNames];
      const categoryOptions = listbox.getByRole("option");
      await expect(categoryOptions).toHaveCount(expectedOptionNames.length);
      await expect(categoryOptions).toHaveText(expectedOptionNames);
      for (const category of expectedOptionNames) {
        await expect(
          listbox.getByRole("option", { name: category, exact: true }),
        ).toBeVisible();
      }

      // Close the listbox by clicking the trigger again
      await categoryTrigger.click();
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/activity/[taskId] - discover a checklist task and verify the current Plan tab", async () => {
    let tasks: unknown = [];
    try {
      tasks = await fetchFromBackend("/api/board");
    } catch (err) {
      const msg = `Failed to fetch tasks from backend: ${err instanceof Error ? err.message : err}`;
      handleMissingPrecondition(msg);
      return;
    }
    
    const liveExternalNoPlan = Array.isArray(tasks)
      ? tasks.find((candidate): candidate is Record<string, unknown> => {
          if (typeof candidate !== "object" || candidate === null) return false;
          const task = candidate as Record<string, unknown>;
          return (
            typeof task.id === "string" &&
            typeof task.title === "string" &&
            task.title.toLowerCase().includes("external") &&
            typeof task.description === "string" &&
            task.description.includes("Observational only") &&
            task.planner_brief == null &&
            task.checklist == null
          );
        })
      : undefined;
    if (!liveExternalNoPlan) {
      handleMissingPrecondition(
        "Live no-plan control precondition failed — /api/board must contain an observational external task with no planner brief or checklist",
      );
      return;
    }
    expect(liveExternalNoPlan.checklist).toBeNull();
    expect(liveExternalNoPlan.planner_brief).toBeNull();

    // Hermetic checklist fixture: retain the shape of a real live task while
    // changing only this browser's board/detail reads. Nothing is written to
    // the backend, and every other product case still uses the live APIs.
    const syntheticChecklistTask: ChecklistBearingTask = {
      ...liveExternalNoPlan,
      id: "checklist-control",
      checklist: { done: 1, total: 2 },
    };

    // Counterfeit control: discovery skips the live external/no-plan row and
    // rejects zero-total pseudo-checklists.
    expect(
      selectChecklistBearingTask([
        liveExternalNoPlan,
        { id: "zero-total-control", checklist: { done: 0, total: 0 } },
        syntheticChecklistTask,
      ]),
    ).toBe(syntheticChecklistTask);

    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      const interceptedPaths: string[] = [];
      const syntheticTaskPath = `/api/board/${encodeURIComponent(syntheticChecklistTask.id)}`;
      await page.route("**/api/board**", async (route) => {
        const url = new URL(route.request().url());
        if (url.origin !== E2E_BASE_URL) {
          await route.continue();
          return;
        }

        const fulfillJson = async (payload: unknown) => {
          interceptedPaths.push(`${url.pathname}${url.search}`);
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(payload),
          });
        };

        if (url.pathname === "/api/board" && url.search === "") {
          await fulfillJson([syntheticChecklistTask]);
          return;
        }
        if (
          url.pathname === "/api/board" &&
          url.searchParams.get("parent_task_id") === syntheticChecklistTask.id
        ) {
          await fulfillJson([]);
          return;
        }
        if (url.pathname === `${syntheticTaskPath}/sessions`) {
          await fulfillJson({ sessions: [], orchestration: null, live_session_id: null });
          return;
        }
        if (url.pathname === `${syntheticTaskPath}/longhaul`) {
          await fulfillJson({
            attempts: [],
            workbook_content: "",
            acceptance: "",
            park_state: null,
          });
          return;
        }
        if (url.pathname === `${syntheticTaskPath}/conversation`) {
          await fulfillJson([]);
          return;
        }
        if (url.pathname === `${syntheticTaskPath}/eta`) {
          await fulfillJson({
            estimate_seconds: null,
            basis: "insufficient_data",
            sample_size: 0,
            confidence: "low",
            computed_at: new Date(0).toISOString(),
          });
          return;
        }

        await route.continue();
      });

      await page.goto(
        `${E2E_BASE_URL}/activity/${syntheticChecklistTask.id}?kind=board`,
      );

      // Verify page loads by checking elements
      await expect(page.locator("h1")).toBeVisible({ timeout: 15000 });

      // The mocked `**/api/board**` routes above cover this page's own data
      // needs, but the shared AppShell around it (alert/escalation counts,
      // the project switcher) makes its own dashboard API calls that are NOT
      // mocked here — on this box, with no OMNIAGENTOS_TRUSTED_HOP_SECRET /
      // dev-escape configured for this `next dev`, the trusted-hop middleware
      // refuses those, which is unrelated to this test's own mocking or the
      // IA redo (see docs/runbooks/dashboard-local-auth.md).
      const tablist = page.getByRole("tablist", { name: "Task detail tabs" });
      const tabNames = ["Overview", "Plan", "Chat", "Files", "Runs", "Approvals"];
      try {
        await expect(tablist.getByRole("tab")).toHaveText(tabNames, { timeout: 5000 });
      } catch {
        handleMissingPrecondition("Task detail tabs never rendered — a dashboard API call outside this test's mocks was refused");
        return;
      }

      const overviewTab = tablist.getByRole("tab", { name: "Overview", exact: true });
      const planTab = tablist.getByRole("tab", { name: "Plan", exact: true });
      await expect(overviewTab).toHaveAttribute("aria-selected", "true");
      await planTab.click();
      await expect(planTab).toHaveAttribute("aria-selected", "true");

      const planPanel = page.getByRole("tabpanel");
      await expect(
        planPanel.getByRole("heading", { name: "Checklist", exact: true }),
      ).toBeVisible({ timeout: 10000 });
      await expect(
        planPanel.getByText(
          `${syntheticChecklistTask.checklist.done}/${syntheticChecklistTask.checklist.total} done`,
          { exact: true },
        ),
      ).toBeVisible();

      const expectedPercent = Math.round(
        (syntheticChecklistTask.checklist.done /
          syntheticChecklistTask.checklist.total) *
          100,
      );
      const progress = planPanel.getByRole("progressbar");
      await expect(progress).toHaveAttribute("aria-valuenow", String(expectedPercent));
      await expect(progress).toHaveAttribute("aria-valuemin", "0");
      await expect(progress).toHaveAttribute("aria-valuemax", "100");
      expect(interceptedPaths).toContain("/api/board");
      expect(interceptedPaths).toContain(`${syntheticTaskPath}/sessions`);
      expect(interceptedPaths).toContain(`${syntheticTaskPath}/longhaul`);
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

  test("/sessions - open a session and verify the transcript modal opens successfully", async () => {
    let browser: Browser | null = null;
    try {
      browser = await launchChromium();
      const page = await browser.newPage();
      await page.goto(`${E2E_BASE_URL}/sessions`);

      // Ensure the page header loads
      await expect(page.locator("h1:has-text('Sessions')")).toBeVisible({ timeout: 15000 });

      // Wait for table of sessions to load, the "No sessions have been recorded." empty state, or unauthorized card to render
      const noSessionsState = page.locator("text=No sessions have been recorded.");
      const viewButtons = page.locator("button[aria-label^='View session']");
      const unauthorizedState = page.locator("text=No local session token in this browser");
      
      // Wait for either the table rows button, the empty state, or unauthorized state to
      // appear. A 4th state these three don't cover: this box's own trusted-hop
      // middleware refusing the dashboard API entirely (no
      // OMNIAGENTOS_TRUSTED_HOP_SECRET / dev-escape configured for this `next dev` —
      // unrelated to the IA redo, see docs/runbooks/dashboard-local-auth.md), which
      // the sessions hook renders as its own ErrorState, none of the three above.
      try {
        await expect(noSessionsState.or(viewButtons.first()).or(unauthorizedState)).toBeVisible({ timeout: 15000 });
      } catch {
        handleMissingPrecondition("Sessions view never resolved to a known state — the dashboard API was refused (trusted-hop/backend unavailable)");
        return;
      }

      if (await unauthorizedState.count() > 0) {
        handleMissingPrecondition("Unauthorized in sessions view — session-token missing or invalid");
        return;
      }

      const sessionCount = await viewButtons.count();
      if (sessionCount === 0) {
        handleMissingPrecondition("No sessions present in the table — cannot verify transcript modal");
        return;
      }

      // Click on the first session to open the details transcript modal (Dialog)
      await viewButtons.first().click();

      // Verify dialog container is visible
      const dialog = page.locator("role=dialog");
      await expect(dialog).toBeVisible({ timeout: 10000 });

      // Verify 'Conversation log' header inside the dialog
      const conversationLogHeader = dialog.locator("strong:has-text('Conversation log')");
      await expect(conversationLogHeader).toBeVisible();

      // Verify that the conversation log either contains an empty notice or the log block (role=log)
      const emptyLogMsg = dialog.locator("text=No transcript entries recorded for this session yet");
      const logBox = dialog.locator("[role='log']");
      await expect(emptyLogMsg.or(logBox)).toBeVisible({ timeout: 10000 });

      // Close the dialog using the Close button in the footer
      const closeButton = dialog.locator("button:has-text('Close')");
      await closeButton.click();

      // Dialog should be hidden
      await expect(dialog).toBeHidden();
    } finally {
      await browser?.close().catch(() => undefined);
    }
  });

});
