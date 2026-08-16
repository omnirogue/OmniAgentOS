# E2E Product QA walk-through guide

This document provides instructions for running and extending the browser-level E2E integration walkthrough specs.

These specs serve as the regression-safety gate for new product surfaces (chats, Kanban board, task detail checklist, and session transcript modals) and run within their own isolated Playwright project (`product-qa`).

---

## 1. What each spec covers

The test suite in `dashboard/e2e/product.spec.ts` covers the following workflows:

1. **/chats**:
   - Verifies that the folder tree accurately renders the seeded organization structure: `Globex`, `Initech`, `AcmeUni`, and `AgentProAcademy` nested under `AcmeUni`.
   - Clicks on a project folder (`Globex`), waits for its conversation list to load, and clicks a conversation card (`Globex Chat`).
   - Types a test message into the text composer area, clicks **Send**, and asserts that the message renders in the conversation stream.
   - Verifies that the `queued — agents read this on their next run` badge immediately renders at the bottom of the message thread.

2. **/board**:
   - Asserts that all 9 target Kanban column groups are rendered on the screen: `Backlog`, `Ready`, `Running`, `Needs you`, `In Review`, `Testing`, `Integration`, `Completed`, and `Blocked`.
   - Locates the custom custom-listbox **Category** dropdown trigger, opens it, and checks that all 5 seeded categories are present: `All categories`, `feature`, `bug`, `refactor`, `docs`, and `ops`.

3. **/activity/[taskId]** (Task details view):
   - Dynamically fetches the board tasks list from the backend to discover a live, active task ID (no brittle, hardcoded IDs).
   - Navigates to `/activity/[taskId]?kind=board` for that task.
   - Asserts that the **Live checklist** section is visible on the screen.
   - Resiliently asserts that either the checklist items are rendered (if the session has todos) or that an appropriate empty state explanation (such as *"no live checklist was recorded"*) is rendered.

4. **/sessions** (Sessions view & dialog):
   - Navigates to `/sessions` and waits for sessions to load.
   - Locates the table rows and clicks on **View session [id]** for the first active session.
   - Verifies that the details transcript dialog opens successfully.
   - Verifies that the **Conversation log** subsection header is present in the dialog.
   - Validates that either the empty log placeholder message or the transcript logs block with `role="log"` is visible.

---

## 2. Ports, Stack configuration, and running the tests

The suite is designed to run against a live stack (port `3002` default for the dashboard and port `8485` for the backend API).

### Step 1: Launch the backend API
Ensure the API backend is running on port `8485`:
```bash
# Usually managed via system launcher or make targets in the workspace
uv run uvicorn omniagentos.api:app --host 127.0.0.1 --port 8485
```

### Step 2: Start the Next.js dev server on port 3002
To prevent port collisions with other Docker or production services, start Next.js using port `3002`:
```bash
cd dashboard
node_modules/.bin/next dev -H 127.0.0.1 -p 3002
```

### Step 3: Run the tests
Execute the walkthrough specs under our dedicated Playwright project:
```bash
cd dashboard
# Run only our E2E walkthrough specs
npx playwright test --project=product-qa

# Run with verbose console logging
npx playwright test --project=product-qa --reporter=list
```

You can target a different dashboard URL by setting the `PLAYWRIGHT_BASE_URL` environment variable:
```bash
PLAYWRIGHT_BASE_URL=http://localhost:3000 npx playwright test --project=product-qa
```

---

## 3. What the skip conditions mean

To uphold **the anti-false-green rule**, our tests will **never** claim success on a blank screen or trivial assertions (`expect(true).toBe(true)`). Instead, if pre-conditions or seeded datasets are missing, the test **skips with an explicit reason string** so the runner and the integrator can see precisely which surface coverage was bypassed:

- **"Globex project folder not found..."**: The project hierarchy needs to be seeded with Globex, Initech, and AcmeUni in the backend database.
- **"Globex Chat conversation card not found..."**: The selected folder does not have any active project-level or task-level conversation history seeded in the database.
- **"Category filter select element not visible..."**: The board page was unable to fetch categories from the `/api/categories` endpoint.
- **"No tasks returned from /api/board..."**: The backend does not have any seeded tasks on the Kanban board to verify.
- **"No sessions present in the table..."**: The backend `/api/sessions` returned no logged-in Claude Code or external agent sessions.
- **"Unauthorized in sessions view..."**: The active session token file `var/secrets/sessions-token` is missing or invalid, preventing access to control plane elements.

---

## 4. How to add a new walkthrough spec

If you need to add a new walkthrough spec for a new product surface, follow these steps:

### Step 1: Write the spec file
All browser walkthrough specs should be added to `dashboard/e2e/product.spec.ts` under the existing description block (or in a new `*.product.spec.ts` file).
Always use the manual Chromium launching pattern to prevent seats/zygote crashes under constrained environments:
```typescript
test("your-new-feature", async () => {
  let browser: Browser | null = null;
  try {
    browser = await launchChromium();
    const page = await browser.newPage();
    await page.goto(`${BASE_URL}/your-route`);
    // Assert and verify your elements ...
  } finally {
    await browser?.close().catch(() => undefined);
  }
});
```

### Step 2: Playwright configuration project registration gotcha
⚠️ **IMPORTANT CRITICAL GOTCHA:**
Playwright is configured with strict, narrowed `testMatch` filters for its projects to keep the EventSource/SSE harness tests isolated.
A new spec file **will not be run** unless it is registered under a project's `testMatch` regex inside `dashboard/playwright.config.ts`.

Our walkthrough tests are matched using:
```typescript
    {
      name: "product-qa",
      testMatch: /product\.spec\.ts$/,
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        launchOptions: {
          chromiumSandbox: false,
          args: [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--single-process",
            "--no-zygote",
            "--disable-gpu",
            "--disable-dev-shm-usage",
          ],
        },
      },
    },
```
Ensure any new walkthrough files you create match the `/product\.spec\.ts$/` pattern (e.g. `feature.product.spec.ts` or similar), or update the project's `testMatch` regex accordingly. Never widen the `testMatch` of existing SSE projects (`chromium` and `node-harness`), as they must remain untouched.
